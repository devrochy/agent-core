"""SPEC-43 · HU-SUB-29/34 · las tools `sub_*` del core de punta a punta, con el proveedor real y la BD.

Cotizar → confirmar → inscribir con un código de promoción, y que `sub_link_pago` apunte al primer cargo de una
inscripción `pendiente_pago`. El cliente sale del contexto y el monto que manda el modelo solo se compara.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from _api_helpers import uniq
from test_subscription_inscripcion import _cargos, _cliente, _negocio, _plan, _sub
from test_subscription_promociones import CODIGO, _promo

import core.subscriptions.tools as tools_mod
from core.shared.tenant import tenant_scope
from core.subscriptions import SubscriptionError

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

DATOS = {"nombre": "Ana Gómez", "email": "ana@ejemplo.co", "telefono": "+573001112233", "acepta_terminos": "tok"}


def _ctx(cliente: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_id=uuid.uuid4(), customer_id=cliente, idempotency_key=uniq("k"), channel="whatsapp"
    )


async def test_cotizar_confirmar_e_inscribir_con_codigo_y_el_link_sirve_el_primer_cargo(monkeypatch) -> None:
    async def _sin_token(_ctx):
        return None

    monkeypatch.setattr(tools_mod, "_token_de_terminos_aceptados", _sin_token)
    tid = await _negocio()
    await _plan(tid, "PREMIUM", "100000")
    await _promo(tid, CODIGO, "20")
    cliente = await _cliente(tid)
    skill = tools_mod.SubscriptionSkill()
    ctx = _ctx(cliente)
    async with tenant_scope(tid):
        listado = await skill.listar_planes({}, ctx)
        assert [p["plan_code"] for p in listado["planes"]] == ["PREMIUM"]
        cot = await skill.cotizar_inscripcion({"plan_code": "PREMIUM", "codigo_promocion": CODIGO}, ctx)
        assert cot["monto_a_pagar"] == 80000.0 and cot["descuento"] == 20000.0
        with pytest.raises(SubscriptionError) as e:  # el monto del modelo se compara, no se usa
            await skill.confirmar_inscripcion(
                {"plan_code": "PREMIUM", "codigo_promocion": CODIGO, "monto_a_pagar": 1}, ctx
            )
        assert e.value.code == "monto_distinto"
        ok = await skill.confirmar_inscripcion(
            {"plan_code": "PREMIUM", "codigo_promocion": CODIGO, "monto_a_pagar": 80000}, ctx
        )
        assert ok["confirmada"]
        r = await skill.inscribir(
            {"plan_code": "PREMIUM", "codigo_promocion": CODIGO, "monto_a_pagar": 80000, "customer_data": dict(DATOS)},
            ctx,
        )
        assert r["estado"] == "pendiente_pago" and r["monto_a_pagar"] == 80000.0
        sub_id = uuid.UUID(r["subscription_id"])
        # `sub_link_pago` apunta al primer cargo de la inscripción pendiente (no crea otro): sin pasarela en el
        # stack de pruebas, el único rechazo posible es el de la pasarela, no «sin cobro pendiente».
        antes = await _cargos(tid, sub_id)
        with pytest.raises(SubscriptionError) as sin_pasarela:
            await skill.link_pago({}, ctx)
        assert sin_pasarela.value.code == "sin_pasarela"
        assert len(await _cargos(tid, sub_id)) == len(antes) == 1
    sub = await _sub(tid, sub_id)
    assert sub.estado == "pendiente_pago" and sub.customer_id == cliente
