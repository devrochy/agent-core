"""SPEC-48, HU-COM-24 · acción `comprobar_comprobante` contra la BD real (la visión, simulada).

- Coincidencia fuerte → pedido `pagado`, evidencia guardada y `order.paid` en el outbox.
- Parcial → `en_revision`; monto menor → `rechazado` (nunca pagado).
- Sin imagen (o una anterior al pedido) → `sin_comprobante`, sin cambios.
- Reintento con el mismo archivo → mismo resultado, sin otra llamada a la visión.
- El mismo archivo en otro pedido → `duplicado`, en revisión.
- Sin cuota → error de negocio para escalar, sin evidencia ni llamada.
- Otro cliente u otro negocio → `pedido_no_encontrado`.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import select, text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

CUENTA = {"entity": "Bancolombia", "accountType": "Ahorros", "accountNumber": "123-456789-01", "holderName": "Tienda"}
# Un JPEG mínimo válido para el perfil de almacenamiento (los bytes cambian por caso: otro sha256).
_JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300") + b"\x08" * 64 + bytes.fromhex("ffd9")


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    await plugin_manager.sync_registry()


@pytest.fixture
def vision(monkeypatch):  # type: ignore[no-untyped-def]
    """Lo que «lee» la visión en cada caso y cuántas veces se llamó."""
    from pack_commerce import comprobantes as cp

    estado: dict[str, Any] = {"leido": {}, "llamadas": 0, "error": None}

    async def _leer(_tid, _contenido, _mime, *, resultado_ledger, moneda="COP"):  # type: ignore[no-untyped-def]
        estado["llamadas"] += 1
        if estado["error"]:
            raise cp.ComprobanteNoLeido(estado["error"])
        return cp.ComprobanteTransferencia.model_validate(estado["leido"])

    monkeypatch.setattr(cp, "leer_comprobante", _leer)
    return estado


async def _negocio_con_agente() -> tuple[uuid.UUID, uuid.UUID]:
    from core.agents.instalaciones_agente import guardar_config_del_agente, instalar_en_agente
    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("transf")
    tid, _ = await onboard_tenant(name=f"T {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False, plan="premium")
    aid = uuid.uuid4()
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                text("insert into agents (id, tenant_id, key, kind, name) values (:a, :t, :k, 'specialist', 'Ventas')"),
                {"a": aid, "t": tid, "k": f"ventas_{uuid.uuid4().hex[:8]}"},
            )
        await instalar_en_agente(tid, aid, "pack_commerce")
        await guardar_config_del_agente(
            tid, aid, "pack_commerce",
            {"payment_forms": [{"method": "transferencia", "enabled": True, "instructions": ""}], "transfer_accounts": [CUENTA]},
        )
    return tid, aid


async def _pedido(tid: uuid.UUID, *, total: int = 85_000, con_imagen: bool = True, imagen: bytes | None = None,
                  imagen_antes: bool = False) -> dict[str, Any]:
    from pack_commerce.models import Order

    from core import almacenamiento
    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope, tenant_slug

    async with tenant_scope(tid):
        slug = await tenant_slug(tid)
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uuid.uuid4().hex[:10]}")
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id)
            session.add(conv)
            await session.flush()
            seq = (await session.execute(text("select coalesce(max(order_seq), 0) + 1 from pack_commerce_orders"))).scalar()
            creado = datetime.now(UTC) - timedelta(minutes=10)
            order = Order(
                tenant_id=tid, conversation_id=conv.id, customer_id=cliente.id, total=total,
                payment_method="transferencia", payment_status="pendiente", idempotency_key=uuid.uuid4().hex,
                order_seq=seq, public_order_number=f"OC-{seq:04d}", created_at=creado,
            )
            session.add(order)
            await session.flush()
            if con_imagen:
                ref = almacenamiento.guardar("media", imagen or (_JPEG + uuid.uuid4().bytes), mimetype="image/jpeg", tenant_slug=slug)
                session.add(Message(
                    tenant_id=tid, conversation_id=conv.id, direction="inbound", sender_type="customer",
                    content="[imagen]", media_type="image/jpeg", media_url=ref.key,
                    created_at=creado - timedelta(minutes=5) if imagen_antes else datetime.now(UTC),
                ))
            return {"order_id": order.id, "customer_id": cliente.id, "conversation_id": conv.id}


async def _comprobar(tid: uuid.UUID, aid: uuid.UUID, p: dict[str, Any], **otro: Any) -> dict[str, Any]:
    from pack_commerce.skills import OrderSkill

    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    ctx = ToolContext(
        conversation_id=otro.get("conversation_id", p["conversation_id"]),
        customer_id=otro.get("customer_id", p["customer_id"]),
        idempotency_key=uuid.uuid4().hex, channel="whatsapp", agent_id=aid,
    )
    async with tenant_scope(tid):
        return await OrderSkill().comprobar_comprobante({"order_id": str(p["order_id"])}, ctx)


async def _estado(tid: uuid.UUID, order_id: uuid.UUID) -> tuple[str, int, int]:
    from pack_commerce.models import Order, PaymentProof

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        order = await session.get(Order, order_id)
        pruebas = (await session.execute(select(PaymentProof).where(PaymentProof.order_id == order_id))).scalars().all()
        eventos = (
            await session.execute(
                text("select count(*) from domain_events where dedupe_key = :k"), {"k": f"order.paid:{order_id}"}
            )
        ).scalar()
    return order.payment_status, len(pruebas), int(eventos or 0)


FUERTE = {"monto": 85000, "fecha": datetime.now(UTC).strftime("%Y-%m-%d"), "estado": "exitoso",
          "cuenta_destino": "****8901", "banco": "Bancolombia", "confianza": 0.95}


async def test_coincidencia_fuerte_paga_guarda_evidencia_y_es_idempotente(vision) -> None:  # type: ignore[no-untyped-def]
    tid, aid = await _negocio_con_agente()
    p = await _pedido(tid)
    vision["leido"] = FUERTE
    r = await _comprobar(tid, aid, p)
    assert r["resultado"] == "pagado" and r["pago_confirmado"] == "confirmado" and r["total"] == 85000
    assert "8901" not in str(r) and "Bancolombia" not in str(r)  # nada de lo que leyó la visión
    assert await _estado(tid, p["order_id"]) == ("pagado", 1, 1)
    # Reintento: no hay otra llamada a la visión ni otra evidencia.
    r2 = await _comprobar(tid, aid, p)
    assert r2["resultado"] == "pagado" and r2.get("repetido") is True
    assert vision["llamadas"] == 1 and (await _estado(tid, p["order_id"]))[1] == 1


async def test_parcial_y_monto_menor(vision) -> None:  # type: ignore[no-untyped-def]
    tid, aid = await _negocio_con_agente()
    p = await _pedido(tid)
    vision["leido"] = {**FUERTE, "cuenta_destino": None, "banco": None}
    assert (await _comprobar(tid, aid, p))["resultado"] == "en_revision"
    assert (await _estado(tid, p["order_id"]))[:2] == ("en_revision", 1)

    q = await _pedido(tid)
    vision["leido"] = {**FUERTE, "monto": 80000}
    assert (await _comprobar(tid, aid, q))["resultado"] == "rechazado"
    assert (await _estado(tid, q["order_id"]))[0] == "rechazado"


async def test_sin_imagen_o_imagen_anterior_al_pedido(vision) -> None:  # type: ignore[no-untyped-def]
    tid, aid = await _negocio_con_agente()
    p = await _pedido(tid, con_imagen=False)
    assert (await _comprobar(tid, aid, p))["error"] == "sin_comprobante"
    q = await _pedido(tid, imagen_antes=True)
    assert (await _comprobar(tid, aid, q))["error"] == "sin_comprobante"
    assert vision["llamadas"] == 0
    assert (await _estado(tid, q["order_id"]))[:2] == ("pendiente", 0)


async def test_el_mismo_comprobante_en_otro_pedido_va_a_revision(vision) -> None:  # type: ignore[no-untyped-def]
    tid, aid = await _negocio_con_agente()
    imagen = _JPEG + b"mismo-archivo"
    p = await _pedido(tid, imagen=imagen)
    vision["leido"] = FUERTE
    assert (await _comprobar(tid, aid, p))["resultado"] == "pagado"
    q = await _pedido(tid, imagen=imagen)
    assert (await _comprobar(tid, aid, q))["resultado"] == "en_revision"


async def test_sin_cuota_escala_sin_evidencia(vision) -> None:  # type: ignore[no-untyped-def]
    tid, aid = await _negocio_con_agente()
    p = await _pedido(tid)
    vision["error"] = "sin_cuota"
    r = await _comprobar(tid, aid, p)
    assert r["error"] == "sin_cuota" and r["escalar"] is True
    assert (await _estado(tid, p["order_id"]))[:2] == ("pendiente", 0)


async def test_otro_cliente_u_otro_negocio_no_comprueban(vision) -> None:  # type: ignore[no-untyped-def]
    tid, aid = await _negocio_con_agente()
    p = await _pedido(tid)
    vision["leido"] = FUERTE
    assert (await _comprobar(tid, aid, p, customer_id=uuid.uuid4()))["error"] == "pedido_no_encontrado"
    tid2, aid2 = await _negocio_con_agente()
    assert (await _comprobar(tid2, aid2, p))["error"] == "pedido_no_encontrado"
    assert vision["llamadas"] == 0
    assert (await _estado(tid, p["order_id"]))[:2] == ("pendiente", 0)


async def test_aislamiento_del_comprobante(vision) -> None:  # type: ignore[no-untyped-def]
    """Auditor multitenant: (a) el mismo archivo en un pedido de OTRO negocio no es duplicado;
    (b) una imagen de otra conversación del mismo cliente no se usa; (c) una `media_url` que apunta a
    la carpeta de otro negocio no se abre."""
    from core.db.models import Conversation, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    imagen = _JPEG + b"compartida-entre-negocios"
    vision["leido"] = FUERTE
    tid, aid = await _negocio_con_agente()
    p = await _pedido(tid, imagen=imagen)
    assert (await _comprobar(tid, aid, p))["resultado"] == "pagado"
    tid2, aid2 = await _negocio_con_agente()
    q = await _pedido(tid2, imagen=imagen)
    assert (await _comprobar(tid2, aid2, q))["resultado"] == "pagado"  # (a) no es duplicado

    # (b) La foto llega por otra conversación del mismo cliente: no cuenta.
    r = await _pedido(tid, con_imagen=False)
    async with tenant_scope(tid), get_session() as session:
        otra = Conversation(tenant_id=tid, customer_id=r["customer_id"])
        session.add(otra)
        await session.flush()
        session.add(Message(
            tenant_id=tid, conversation_id=otra.id, direction="inbound", sender_type="customer",
            content="[imagen]", media_type="image/jpeg", media_url="x/media/otra.jpg", created_at=datetime.now(UTC),
        ))
    assert (await _comprobar(tid, aid, r))["error"] == "sin_comprobante"

    # (c) Una key limpia de la carpeta de otro negocio no se abre.
    from core import almacenamiento
    from core.shared.tenant import tenant_slug

    s = await _pedido(tid, con_imagen=False)
    async with tenant_scope(tid2):
        slug2 = await tenant_slug(tid2)
    ajena = almacenamiento.guardar("media", _JPEG + b"ajena", mimetype="image/jpeg", tenant_slug=slug2)
    async with tenant_scope(tid), get_session() as session:
        session.add(Message(
            tenant_id=tid, conversation_id=s["conversation_id"], direction="inbound", sender_type="customer",
            content="[imagen]", media_type="image/jpeg", media_url=ajena.key, created_at=datetime.now(UTC),
        ))
    llamadas = vision["llamadas"]
    assert (await _comprobar(tid, aid, s))["error"] == "sin_comprobante"
    assert vision["llamadas"] == llamadas
