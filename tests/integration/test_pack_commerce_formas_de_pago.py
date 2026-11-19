"""SPEC-48, HU-COM-21 · `GET/PUT /api/v1/pack/commerce/agents/{id}/payment-forms`, contra la BD real.

- Sin configurar: contra entrega activa; transferencia y pago en línea se deducen como antes.
- Transferencia encendida sin cuenta se guarda y queda «Falta configurar»; con cuenta, activa.
- No se puede apagar todo; una cuenta sin número se rechaza.
- Las formas son del agente: otro agente del negocio no cambia; otro negocio no ve el agente (404).
- `crear_pedido` rechaza una forma que no está activa para el agente.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

CUENTA = {"entity": "Bancolombia", "accountType": "Ahorros", "accountNumber": "123-456789-01", "holderName": "Tienda"}


def _ruta(aid: uuid.UUID) -> str:
    return f"/api/v1/pack/commerce/agents/{aid}/payment-forms"


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> None:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        await session.execute(text(sql), params)


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    await plugin_manager.sync_registry()


async def _negocio_con_dos_agentes() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    from core.agents.instalaciones_agente import instalar_en_agente
    from core.alta_negocio import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = uniq("formas")
    tid, _ = await onboard_tenant(name=f"F {slug}", slug=slug, packs=["pack_commerce"], sembrar_ejemplos=False, plan="premium")
    agentes = []
    for _ in range(2):
        aid = uuid.uuid4()
        await _sql(
            tid,
            "insert into agents (id, tenant_id, key, kind, name) values (:a, :t, :k, 'specialist', 'Ventas')",
            a=aid, t=tid, k=f"ventas_{uuid.uuid4().hex[:8]}",
        )
        async with tenant_scope(tid):
            await instalar_en_agente(tid, aid, "pack_commerce")
        agentes.append(aid)
    return tid, agentes[0], agentes[1]


def _estado(cuerpo: dict) -> dict[str, str]:
    return {f["method"]: f["estado"] for f in cuerpo["forms"]}


async def test_formas_del_agente_de_punta_a_punta(app_client) -> None:  # type: ignore[no-untyped-def]
    tid, aid, otro = await _negocio_con_dos_agentes()
    h = await api_headers(app_client, tid)

    r = await app_client.get(_ruta(aid), headers=h)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["configurada"] is False
    assert _estado(cuerpo)["efectivo_contraentrega"] == "activa"
    assert next(f for f in cuerpo["forms"] if f["method"] == "efectivo_contraentrega")["superpoder"] is None

    # Transferencia sin cuenta: se guarda y queda «Falta configurar».
    formas = [{"method": "efectivo_contraentrega", "enabled": True}, {"method": "transferencia", "enabled": True}]
    r = await app_client.put(_ruta(aid), headers=h, json={"forms": formas})
    assert r.status_code == 200, r.text
    assert _estado(r.json())["transferencia"] == "falta_configurar"

    # Con cuenta queda activa.
    r = await app_client.put(_ruta(aid), headers=h, json={"forms": formas, "transfer_accounts": [CUENTA]})
    assert r.status_code == 200, r.text
    assert _estado(r.json())["transferencia"] == "activa"
    assert r.json()["transfer_accounts"][0]["accountNumber"] == CUENTA["accountNumber"]

    # El otro agente del mismo negocio no cambió.
    r = await app_client.get(_ruta(otro), headers=h)
    assert r.json()["configurada"] is False and _estado(r.json())["transferencia"] == "apagada"

    # El runtime del agente solo acepta lo activo.
    from pack_commerce.formas_de_pago import metodos_del_agente

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        assert await metodos_del_agente(aid) == ["efectivo_contraentrega", "transferencia"]
        assert await metodos_del_agente(otro) == ["efectivo_contraentrega"]


async def test_validaciones(app_client) -> None:  # type: ignore[no-untyped-def]
    tid, aid, _ = await _negocio_con_dos_agentes()
    h = await api_headers(app_client, tid)
    r = await app_client.put(_ruta(aid), headers=h, json={"forms": [{"method": "efectivo_contraentrega", "enabled": False}]})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_payment_forms"
    r = await app_client.put(
        _ruta(aid), headers=h,
        json={"forms": [{"method": "transferencia", "enabled": True}], "transfer_accounts": [{"entity": "Nequi", "accountNumber": ""}]},
    )
    assert r.status_code == 400
    r = await app_client.put(
        _ruta(aid), headers=h,
        json={"forms": [{"method": "efectivo_contraentrega", "enabled": True}], "transfer_proof_required": False, "transfer_ocr_enabled": True},
    )
    assert r.status_code == 400


async def test_otro_negocio_no_ve_ni_cambia_el_agente(app_client) -> None:  # type: ignore[no-untyped-def]
    _tid, aid, _ = await _negocio_con_dos_agentes()
    tid2, _, _ = await _negocio_con_dos_agentes()
    h2 = await api_headers(app_client, tid2)
    assert (await app_client.get(_ruta(aid), headers=h2)).status_code == 404
    r = await app_client.put(_ruta(aid), headers=h2, json={"forms": [{"method": "efectivo_contraentrega", "enabled": True}]})
    assert r.status_code == 404
    assert (await app_client.get(_ruta(uuid.uuid4()), headers=h2)).status_code == 404
