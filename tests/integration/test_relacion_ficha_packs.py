"""HU-CRM-14 (ADR-0179): ficha y detalle por módulo, con datos reales, y la tool de historial."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from test_api_clientes_agentes import _cliente_con_data, _negocio

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

import pack_calendar.providers  # noqa: E402, F401  (registran sus LeadProvider)
import pack_cobranza.providers  # noqa: E402, F401
import pack_commerce.providers  # noqa: E402, F401

PACKS = {"pack_commerce", "pack_cobranza", "pack_calendar"}


async def test_la_ficha_resume_cada_modulo_sin_montos_ni_datos_personales() -> None:
    from core.memory import relacion
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente_id, _ = await _cliente_con_data(tid)
    async with tenant_scope(tid):
        bloque = await relacion.para_prompt(cliente_id, PACKS)
    assert "Relación de este cliente con el negocio" in bloque
    assert "1 pedido(s)" in bloque and "cita próxima" in bloque
    assert "1 cuota(s) atrasada(s)" in bloque and "promesa de pago pendiente" in bloque
    for prohibido in ("120000", "120.000", "80000", "80.000", "$", "Ana"):
        assert prohibido not in bloque, prohibido


async def test_un_modulo_no_habilitado_no_aporta_su_linea() -> None:
    from core.memory import relacion
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente_id, _ = await _cliente_con_data(tid)
    async with tenant_scope(tid):
        bloque = await relacion.para_prompt(cliente_id, {"pack_calendar"})
    assert "cita próxima" in bloque and "pedido(s)" not in bloque and "cuota" not in bloque


async def test_un_cliente_sin_datos_en_los_modulos_no_trae_bloque() -> None:
    from core.memory import relacion
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        assert await relacion.para_prompt(uuid.uuid4(), PACKS) == ""


async def test_la_tool_trae_el_detalle_solo_del_cliente_de_la_conversacion() -> None:
    from core.memory.historial_tool import HistorialClienteSkill
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente_id, _ = await _cliente_con_data(tid)
    otro_negocio = await _negocio()
    otro_cliente, _ = await _cliente_con_data(otro_negocio)
    skill = HistorialClienteSkill()
    ctx = SimpleNamespace(customer_id=cliente_id)
    async with tenant_scope(tid):
        modulos = await skill.consultar({}, ctx)
        ventas = await skill.consultar({"modulo": "ventas", "limite": 99}, ctx)
        agenda = await skill.consultar({"modulo": "AGENDA"}, ctx)
        cobranza = await skill.consultar({"modulo": "cobranza"}, ctx)
        inexistente = await skill.consultar({"modulo": "suscripciones"}, ctx)
        # Un cliente de OTRO negocio no se ve aunque se conozca su id.
        ajeno = await skill.consultar({"modulo": "ventas"}, SimpleNamespace(customer_id=otro_cliente))
    assert {"ventas", "agenda", "cobranza"} <= set(modulos["modulos"])
    [pedido] = ventas["registros"]
    assert pedido["tipo"] == "pedido" and pedido["total"] == 120000
    assert agenda["registros"][0]["tipo"] == "cita"
    tipos = {r["tipo"] for r in cobranza["registros"]}
    assert tipos == {"prestamo", "promesa"}
    assert "error" in inexistente
    assert ajeno["registros"] == []
