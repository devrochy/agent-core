"""HU-AOS-71 · preset propio de la plataforma (`plataforma-suscripciones`) y preset genérico intacto.

- **Siembra del salto sin Ventas:** el negocio nace con Suscripciones como agente por defecto, con Captar
  leads, y Agenda para las demos; sin agente Ventas y sin `pack_commerce`.
- **Preset genérico intacto:** `suscripciones` sigue naciendo con comercio y con Ventas, Agenda y
  Suscripciones.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


@pytest.fixture(scope="module", autouse=True)
def _alta_con_agent_os():
    """El alta real (Coordinador con la conexión y agentes del preset), como `test_alta_agentes_base`."""
    import _alta_agent_os

    _alta_agent_os.ACTIVO = True
    yield
    _alta_agent_os.ACTIVO = False


@pytest.fixture(scope="module", autouse=True)
async def _proceso() -> None:
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()


async def _negocio_con_preset(preset: str) -> tuple[uuid.UUID, Any]:
    """El alta real: negocio con su número de WhatsApp y el preset. Desde ADR-0205 §1 el alta no crea
    especialistas (solo el Coordinador): los agentes del preset se aplican aquí a mano, como
    `agent-core db presets-v2`, para revisar lo que el preset sugiere."""
    from _habilidades import onboard_tenant

    from core.presets import load_preset
    from core.presets.agentes import aplicar_agentes
    from core.shared.tenant import tenant_scope

    slug = f"aos71-{uuid.uuid4().hex[:8]}"
    tid, pasos = await onboard_tenant(
        name=f"AOS71 {slug}", slug=slug, preset=preset, plan="premium", sembrar_ejemplos=False,
        whatsapp={"phone_number_id": f"pn-{uuid.uuid4().hex[:12]}", "app_secret": "x", "access_token": "x",
                  "verify_token": "x"},
    )
    assert await _sql(tid, "select key from agents where tenant_id = :t and kind = 'specialist'", t=tid) == []
    async with tenant_scope(tid):
        await aplicar_agentes(tid, load_preset(preset))
    registro = await _sql(tid, "select settings -> 'preset' from tenants where id = :t", t=tid)
    return tid, (pasos, registro[0][0])


async def _packs(tid: uuid.UUID) -> set[str]:
    from core.agents.instalaciones_agente import packs_del_negocio
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):  # las de sus agentes activos (HU-AOS-91)
        return await packs_del_negocio(tid)


async def test_preset_propio_nace_sin_comercio_ni_ventas_con_suscripciones_por_defecto() -> None:
    from core.agents.instancias import especialista_por_defecto
    from core.db.session import get_session
    from core.leads.tools import es_de_captar_leads
    from core.shared.tenant import tenant_scope
    from core.subscriptions import proveedor_configurado

    tid, (pasos, registro) = await _negocio_con_preset("plataforma-suscripciones")

    assert registro["id"] == "plataforma-suscripciones", pasos
    agentes = await _sql(tid, "select key from agents where tenant_id = :t and deleted_at is null", t=tid)
    assert {"suscripciones", "agenda"} <= {k for (k,) in agentes}, pasos
    assert "pack_commerce" not in await _packs(tid)
    assert "pack_subscriptions" in await _packs(tid)
    assert await proveedor_configurado(tid) == "pack_subscriptions"

    assert "ventas" not in {k for (k,) in agentes}

    async with tenant_scope(tid):
        async with get_session() as session:
            defecto = await especialista_por_defecto(session, tid)
    suscripciones = await _sql(tid, "select id from agents where tenant_id = :t and key = 'suscripciones'", t=tid)
    assert defecto == suscripciones[0][0]

    contenido = await _sql(
        tid,
        "select v.content from agent_versions v join agents a on a.id = v.agent_id "
        "where v.tenant_id = :t and a.key = 'suscripciones' order by v.version desc limit 1",
        t=tid,
    )
    tools = {b["tool"] for b in contenido[0][0]["tools"]}
    assert any(es_de_captar_leads(t) for t in tools), tools


async def test_preset_generico_suscripciones_sigue_con_comercio_y_ventas() -> None:
    tid, (pasos, _) = await _negocio_con_preset("suscripciones")

    agentes = await _sql(tid, "select key from agents where tenant_id = :t and deleted_at is null", t=tid)
    assert {"ventas", "agenda", "suscripciones"} <= {k for (k,) in agentes}, "\n".join(pasos)
    assert "pack_commerce" in await _packs(tid)
