"""HU-SUB-26 · registrar el pago por transferencia de un cargo, contra la BD real.

El dueño marca pagado un cobro que el cliente transfirió: el cargo queda `pagado` con
`pago_externo.canal = transferencia`, la suscripción se reactiva, sale `subscription.charge_paid`
una vez y el pack sigue cobrando los períodos siguientes. Aislamiento y permisos como el resto
de las acciones del panel.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from _api_helpers import api_headers
from sqlalchemy import select
from test_subscriptions_admin_ui import (
    BASE,
    PASSWORD,
    _auditoria,
    _cargo,
    _cliente,
    _colaborador,
    _eventos,
    _negocio,
    _sub,
)

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _leer(tid, cargo_id, sub_id):
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cargo = (await session.execute(select(SubscriptionCharge).where(SubscriptionCharge.id == cargo_id))).scalar_one()
            sub = (await session.execute(select(Subscription).where(Subscription.id == sub_id))).scalar_one()
            session.expunge_all()
            return cargo, sub


async def _vencida(tid, *, estado="vencida", **campos):
    cliente = await _cliente(tid, "Ana Gómez")
    sid = await _sub(tid, cliente, estado=estado, valor="80000", proximo_cargo=None, **campos)
    cid = await _cargo(tid, sid, vence=datetime.now(UTC) - timedelta(days=5), monto="80000", link=None)
    return sid, cid


async def test_registra_la_transferencia_y_reactiva_la_suscripcion(app_client) -> None:
    tid = await _negocio()
    sid, cid = await _vencida(tid)
    h = await api_headers(app_client, tid)

    r = await app_client.post(f"{BASE}/cargos/{cid}/registrar-pago", json={"referencia": "Comprobante 99812"}, headers=h)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["estado"] == "registrado" and cuerpo["suscripcion_estado"] == "activa"
    assert cuerpo["proximo_cargo"] is not None and cuerpo["charge_id"] == str(cid)

    cargo, sub = await _leer(tid, cid, sid)
    assert cargo.estado == "pagado"
    assert cargo.pago_externo["canal"] == "transferencia" and cargo.pago_externo["referencia"] == "Comprobante 99812"
    assert sub.estado == "activa" and sub.proximo_cargo is not None
    [pagado] = await _eventos(tid, "subscription.charge_paid")
    assert pagado.payload["charge_id"] == str(cid)
    [fila] = await _auditoria(tid, "subs_pago_transferencia_registrado")
    assert fila.actor == "admin:admin@local" and fila.input["referencia"] == "Comprobante 99812"


async def test_pausada_por_mora_se_reactiva_y_pausada_por_el_cliente_sigue_pausada(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    sid_mora, cid_mora = await _vencida(tid, estado="pausada", motivo_pausa="mora")
    sid_cli, cid_cli = await _vencida(tid, estado="pausada", motivo_pausa="cliente")
    for cid in (cid_mora, cid_cli):
        assert (await app_client.post(f"{BASE}/cargos/{cid}/registrar-pago", json={"referencia": "ref-123"}, headers=h)).status_code == 200
    assert (await _leer(tid, cid_mora, sid_mora))[1].estado == "activa"
    assert (await _leer(tid, cid_cli, sid_cli))[1].estado == "pausada"  # su pausa la pidió él


async def test_repetir_la_misma_referencia_es_idempotente(app_client) -> None:
    tid = await _negocio()
    _, cid = await _vencida(tid)
    h = await api_headers(app_client, tid)
    primero = await app_client.post(f"{BASE}/cargos/{cid}/registrar-pago", json={"referencia": "ref-123"}, headers=h)
    segundo = await app_client.post(f"{BASE}/cargos/{cid}/registrar-pago", json={"referencia": "ref-123"}, headers=h)
    assert primero.json()["estado"] == "registrado" and segundo.status_code == 200
    assert segundo.json()["estado"] == "ya_registrado"
    assert len(await _eventos(tid, "subscription.charge_paid")) == 1
    assert len(await _auditoria(tid, "subs_pago_transferencia_registrado")) == 1


async def test_otra_referencia_sobre_un_cargo_pagado_es_409(app_client) -> None:
    tid = await _negocio()
    _, cid = await _vencida(tid)
    h = await api_headers(app_client, tid)
    await app_client.post(f"{BASE}/cargos/{cid}/registrar-pago", json={"referencia": "ref-123"}, headers=h)
    r = await app_client.post(f"{BASE}/cargos/{cid}/registrar-pago", json={"referencia": "ref-999"}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "cargo_ya_pagado"


async def test_no_registra_cargos_anulados_de_cambio_de_plan_ni_de_suscripciones_canceladas(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    cliente = await _cliente(tid, "Ana")
    sid = await _sub(tid, cliente, valor="80000")
    anulado = await _cargo(tid, sid, vence=datetime.now(UTC), estado="anulado", link=None, periodo="2026-01")
    cambio = await _cargo(tid, sid, vence=datetime.now(UTC), tipo="cambio_plan", link=None, periodo="2026-02")
    sid_c = await _sub(tid, await _cliente(tid, "Beto"), estado="cancelada", proximo_cargo=None)
    cancelada = await _cargo(tid, sid_c, vence=datetime.now(UTC), link=None)
    esperado = {anulado: "cargo_anulado", cambio: "cargo_no_registrable", cancelada: "suscripcion_cancelada"}
    for cid, codigo in esperado.items():
        r = await app_client.post(f"{BASE}/cargos/{cid}/registrar-pago", json={"referencia": "ref-123"}, headers=h)
        assert r.status_code == 409 and r.json()["error"]["code"] == codigo, (codigo, r.text)
    assert await _eventos(tid, "subscription.charge_paid") == []


async def test_referencia_muy_corta_es_422(app_client) -> None:
    tid = await _negocio()
    _, cid = await _vencida(tid)
    h = await api_headers(app_client, tid)
    for referencia in ("", "ab", "   a "):
        r = await app_client.post(f"{BASE}/cargos/{cid}/registrar-pago", json={"referencia": referencia}, headers=h)
        assert r.status_code == 422, referencia


async def test_aislamiento_entre_negocios(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    sid, cid = await _vencida(a)
    hb = await api_headers(app_client, b)
    r = await app_client.post(f"{BASE}/cargos/{cid}/registrar-pago", json={"referencia": "ref-123"}, headers=hb)
    assert r.status_code == 404
    cargo, _ = await _leer(a, cid, sid)
    assert cargo.estado == "pendiente" and cargo.pago_externo is None
    assert await _auditoria(a, "subs_pago_transferencia_registrado") == []


async def test_colaborador_de_solo_lectura_no_registra(app_client) -> None:
    tid = await _negocio()
    _, cid = await _vencida(tid)
    h = await api_headers(app_client, tid, email=await _colaborador(tid), password=PASSWORD)
    r = await app_client.post(f"{BASE}/cargos/{cid}/registrar-pago", json={"referencia": "ref-123"}, headers=h)
    assert r.status_code == 403


async def test_el_pack_sigue_cobrando_despues_de_una_transferencia_pero_no_despues_de_otro_canal() -> None:
    """`paga_por_otro_canal` apaga link y avisos solo para el canal de la plataforma (HU-SH-04)."""
    from pack_subscriptions.jobs import paga_por_otro_canal
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    resultados = {}
    for canal in ("transferencia", "panel"):
        sid, cid = await _vencida(tid)
        async with tenant_scope(tid):
            async with get_session() as session:
                cargo = await session.get(SubscriptionCharge, cid)
                cargo.estado = "pagado"
                cargo.pago_externo = {"canal": canal, "referencia": "r-1"}
                sub = (await _leer(tid, cid, sid))[1]
                resultados[canal] = await paga_por_otro_canal(session, tid, sub)
    assert resultados == {"transferencia": False, "panel": True}
