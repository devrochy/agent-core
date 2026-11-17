"""SPEC-48, HU-COM-22 · comprobantes del pedido y revisión manual del pago, contra la BD real.

- `en_revision` es un estado de pago válido (filtro de pedidos).
- `GET /orders/{id}/payment-proofs` lista los comprobantes con su resultado.
- Confirmar un comprobante deja el pedido `pagado` con quién y cuándo; rechazar exige motivo y deja
  `rechazado`; un pedido pagado no se rechaza; marcar pagado no necesita comprobante.
- La tabla tiene RLS forzada; otro negocio no ve ni cambia el pedido (404).
- `(order_id, image_sha256)` es único: el mismo comprobante no se guarda dos veces.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

SHA = hashlib.sha256(b"comprobante").hexdigest()


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    await plugin_manager.sync_registry()


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("comprob")
    tid, _ = await onboard_tenant(name=f"C {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False, plan="premium")
    return tid


async def _pedido_con_comprobante(tid: uuid.UUID, estado: str = "en_revision") -> tuple[uuid.UUID, uuid.UUID]:
    from pack_commerce.models import Order, PaymentProof

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        seq = (await session.execute(text("select coalesce(max(order_seq), 0) + 1 from pack_commerce_orders"))).scalar()
        order = Order(
            tenant_id=tid, conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(), total=50_000,
            payment_method="transferencia", payment_status=estado, idempotency_key=uuid.uuid4().hex,
            order_seq=seq, public_order_number=f"OC-{seq:04d}",
        )
        session.add(order)
        await session.flush()
        proof = PaymentProof(
            tenant_id=tid, order_id=order.id, image_key=f"t/{uuid.uuid4().hex}.jpg", image_sha256=SHA,
            extracted={"monto": 50_000, "cuenta_destino": "8901"}, match={"motivos": ["sin_cuenta"]}, status="en_revision",
        )
        session.add(proof)
        await session.flush()
        return order.id, proof.id


def _ruta(order_id: uuid.UUID, sufijo: str) -> str:
    return f"/api/v1/pack/commerce/orders/{order_id}/{sufijo}"


async def test_revision_manual_de_punta_a_punta(app_client) -> None:  # type: ignore[no-untyped-def]
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    order_id, proof_id = await _pedido_con_comprobante(tid)

    r = await app_client.get("/api/v1/pack/commerce/orders", headers=h, params={"pago": "en_revision"})
    assert r.status_code == 200, r.text
    assert any(o["id"] == str(order_id) for o in r.json())

    r = await app_client.get(_ruta(order_id, "payment-proofs"), headers=h)
    assert r.status_code == 200, r.text
    [item] = r.json()["items"]
    assert item["status"] == "en_revision" and item["extracted"]["cuenta_destino"] == "8901"

    r = await app_client.post(_ruta(order_id, "payment-status"), headers=h, json={"accion": "rechazar", "proof_id": str(proof_id)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "missing_reason"

    r = await app_client.post(_ruta(order_id, "payment-status"), headers=h, json={"accion": "confirmar", "proof_id": str(proof_id)})
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["payment_status"] == "pagado" and cuerpo["items"][0]["status"] == "pagado"
    assert cuerpo["payment_status_changed_at"] is not None and cuerpo["items"][0]["reviewed_at"] is not None

    r = await app_client.post(
        _ruta(order_id, "payment-status"), headers=h, json={"accion": "rechazar", "proof_id": str(proof_id), "nota": "x"}
    )
    assert r.status_code == 409


async def test_rechazar_y_marcar_pagado(app_client) -> None:  # type: ignore[no-untyped-def]
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    order_id, proof_id = await _pedido_con_comprobante(tid)
    r = await app_client.post(
        _ruta(order_id, "payment-status"), headers=h,
        json={"accion": "rechazar", "proof_id": str(proof_id), "nota": "El monto no coincide"},
    )
    assert r.status_code == 200 and r.json()["payment_status"] == "rechazado"
    assert r.json()["payment_status_reason"] == "El monto no coincide"

    otro, _ = await _pedido_con_comprobante(tid, estado="pendiente")
    r = await app_client.post(_ruta(otro, "payment-status"), headers=h, json={"accion": "marcar_pagado"})
    assert r.status_code == 200 and r.json()["payment_status"] == "pagado"
    r = await app_client.post(_ruta(otro, "payment-status"), headers=h, json={"accion": "confirmar"})
    assert r.status_code == 400


async def test_otro_negocio_no_ve_ni_cambia_el_pedido(app_client) -> None:  # type: ignore[no-untyped-def]
    tid = await _negocio()
    order_id, proof_id = await _pedido_con_comprobante(tid)
    tid2 = await _negocio()
    h2 = await api_headers(app_client, tid2)
    assert (await app_client.get(_ruta(order_id, "payment-proofs"), headers=h2)).status_code == 404
    r = await app_client.post(_ruta(order_id, "payment-status"), headers=h2, json={"accion": "confirmar", "proof_id": str(proof_id)})
    assert r.status_code == 404

    # RLS forzada: desde el negocio 2 la tabla no muestra el comprobante del 1.
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid2), get_session() as session:
        visibles = (await session.execute(text("select count(*) from pack_commerce_payment_proofs where id = :p"), {"p": proof_id})).scalar()
    assert visibles == 0


async def test_el_mismo_comprobante_no_se_guarda_dos_veces() -> None:
    from pack_commerce.models import PaymentProof

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    order_id, _ = await _pedido_con_comprobante(tid)
    with pytest.raises(IntegrityError):
        async with tenant_scope(tid), get_session() as session:
            session.add(PaymentProof(tenant_id=tid, order_id=order_id, image_key="t/x.jpg", image_sha256=SHA, status="en_revision"))
            await session.flush()
