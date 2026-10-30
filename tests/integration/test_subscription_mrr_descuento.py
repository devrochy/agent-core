"""SPEC-43 · HU-SUB-36 · MRR nominal con la línea de descuento vigente, contra la BD real."""

from __future__ import annotations

import os
import uuid

import pytest
from test_subscription_descuento_n_periodos import _activa_con_descuento, _correr_cron
from test_subscription_inscripcion import _cliente, _plan
from test_subscription_promociones import _con_codigo

from core.db.session import get_session
from core.shared.tenant import tenant_scope

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _metricas(tid: uuid.UUID):
    from pack_subscriptions import consultas

    async with tenant_scope(tid):
        async with get_session() as session:
            return await consultas.metricas(session, tid)


async def test_el_mrr_es_nominal_y_el_descuento_vigente_va_en_su_propia_linea() -> None:
    tid, *_ = await _activa_con_descuento(periodos=3)  # 100.000 al 50 %
    m = await _metricas(tid)
    assert (m.mrr, m.mrr_descuento, m.suscripciones_con_descuento) == (100_000, 50_000, 1)


async def test_sin_descuento_vigente_la_linea_es_cero_y_cuando_se_agota_desaparece() -> None:
    tid, _, _, _, sub_id = await _activa_con_descuento(periodos=2)
    assert (await _metricas(tid)).mrr_descuento == 50_000
    await _correr_cron(tid, sub_id)  # el período 2 consume el último: el descuento se agota
    from test_subscription_descuento_n_periodos import _pago, _periodos

    [cargo] = await _periodos(tid, sub_id)
    await _pago(tid, cargo.id, float(cargo.monto))
    m = await _metricas(tid)
    assert (m.mrr, m.mrr_descuento, m.suscripciones_con_descuento) == (100_000, 0, 0)


async def test_una_inscripcion_pendiente_de_pago_no_suma_ni_al_mrr_ni_al_descuento() -> None:
    from test_subscription_promociones import _escenario

    tid, _, _ = await _escenario()
    cliente = await _cliente(tid)
    await _con_codigo(tid, cliente)  # inscripción con código sin pagar
    m = await _metricas(tid)
    assert (m.mrr, m.mrr_descuento, m.suscripciones_con_descuento, m.pendientes_de_pago) == (0, 0, 0, 1)


async def test_el_descuento_de_un_plan_trimestral_se_normaliza_a_mes() -> None:
    from test_subscription_inscripcion import _cargos, _negocio, _pago
    from test_subscription_promociones import _promo

    tid = await _negocio()
    await _plan(tid, "TRIM", "300000", periodicidad="trimestral")
    await _promo(tid, valor="20", periodos=2)
    cliente = await _cliente(tid)
    vista = await _con_codigo(tid, cliente, plan="TRIM")
    [cargo] = await _cargos(tid, uuid.UUID(vista["subscription_id"]))
    await _pago(tid, cargo.id, float(cargo.monto))
    m = await _metricas(tid)
    assert (m.mrr, m.mrr_descuento) == (100_000, 20_000)  # 300.000 ÷ 3; 20 % de 300.000 ÷ 3
