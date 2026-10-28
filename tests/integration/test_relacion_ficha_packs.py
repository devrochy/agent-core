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


@pytest.fixture(autouse=True)
def _tiempo_holgado(monkeypatch) -> None:
    """Estos tests miran el contenido, no el tiempo: el primer acceso a una base fría puede tardar."""
    from core.memory import relacion

    monkeypatch.setattr(relacion, "TIMEOUT_POR_MODULO_S", 10.0)


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


async def test_cancelar_una_ficha_lenta_a_mitad_de_consulta_no_envenena_el_pool(monkeypatch) -> None:
    """El timeout cancela la ficha con la consulta en vuelo: la siguiente consulta sigue andando."""
    from sqlalchemy import text

    from core.admin.lead_registry import LeadProvider, LeadRegistry, LeadRelacion
    from core.db.session import get_session
    from core.memory import relacion
    from core.shared.tenant import tenant_scope

    async def lenta(ids):
        async with get_session() as s:
            await s.execute(text("select pg_sleep(2)"))
        return {ids[0]: "no llega"}

    async def _vacio(ids):
        return None

    reg = LeadRegistry()
    reg._providers["pack_lento"] = LeadProvider(
        plugin_id="pack_lento", agent_name="Lento", relacion=LeadRelacion(con_datos=_vacio, ficha=lenta)
    )
    monkeypatch.setattr(relacion, "lead_registry", reg)
    monkeypatch.setattr(relacion, "TIMEOUT_POR_MODULO_S", 0.3)
    tid = await _negocio()
    async with tenant_scope(tid):
        for _ in range(3):  # varias veces: si cada cancelación dejara una conexión rota, el pool se agotaría
            assert await relacion.para_prompt(uuid.uuid4(), {"pack_lento"}) == ""
        async with get_session() as s:
            assert (await s.execute(text("select 1"))).scalar() == 1


async def test_la_tool_de_historial_nace_apagada_y_se_ofrece_al_encender_las_herramientas(app_client) -> None:
    from _api_helpers import api_headers

    from core.memory.historial_tool import _hay_modulos
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    async with tenant_scope(tid):
        assert await _hay_modulos(set()) is False
    r = await app_client.put("/api/v1/admin/datos-cliente/politica", headers=h, json={"herramientas_del_agente": True})
    assert r.status_code == 200, r.text
    async with tenant_scope(tid):
        assert await _hay_modulos(set()) is True
