"""Sandbox de la prueba del panel (HU-ARCH-14).

Sin base de datos: el host no ejecuta handlers con efectos en `channel == "test"`, la
auditoría exige que cada tool declare lectura o un simulador, y el número simulado no
usa el consecutivo real.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.mcp.host import ejecutar_tool
from core.mcp.sandbox import SandboxPrueba, numeracion
from core.mcp.types import MCPTool, ToolContext
from core.shared.test_mode import is_panel_test, is_test_channel


def _contexto(channel: str = "test") -> ToolContext:
    conversation_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    return ToolContext(
        conversation_id=conversation_id,
        customer_id=customer_id,
        idempotency_key="k",
        channel=channel,
        sandbox=SandboxPrueba(uuid.uuid4(), conversation_id) if channel == "test" else None,
    )


@pytest.mark.asyncio
async def test_una_tool_con_efectos_usa_el_simulador_y_no_el_handler() -> None:
    llamado = {"handler": False}

    async def handler(_args: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        llamado["handler"] = True
        return {"creado": True}

    async def simular(_args: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        return {"creado": False, "fake": "PRUEBA-0001"}

    tool = MCPTool(
        name="crear_pedido",
        description="x",
        parameters={"type": "object"},
        handler=handler,
        side_effects=True,
        simular=simular,
    )
    result = await ejecutar_tool(tool, {}, _contexto())
    assert result["simulado"] is True and result["fake"] == "PRUEBA-0001"
    assert llamado["handler"] is False


@pytest.mark.asyncio
async def test_una_tool_con_efectos_sin_simulador_falla_cerrado() -> None:
    llamado = {"handler": False}

    async def handler(_args: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        llamado["handler"] = True
        return {"creado": True}

    tool = MCPTool(
        name="peligrosa",
        description="x",
        parameters={"type": "object"},
        handler=handler,
        side_effects=True,
    )
    result = await ejecutar_tool(tool, {}, _contexto())
    assert result["error"] == "herramienta_sin_simulador" and result["simulado"] is True
    assert llamado["handler"] is False


@pytest.mark.asyncio
async def test_permite_en_prueba_ejecuta_el_handler_real() -> None:
    async def handler(_args: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        return {"status": "ok"}

    tool = MCPTool(
        name="update_customer_profile",
        description="x",
        parameters={"type": "object"},
        handler=handler,
        side_effects=True,
        permite_en_prueba=True,
    )
    result = await ejecutar_tool(tool, {}, _contexto())
    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_fuera_de_test_la_tool_corre_normal() -> None:
    async def handler(_args: dict[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        return {"real": True}

    tool = MCPTool(
        name="real",
        description="x",
        parameters={"type": "object"},
        handler=handler,
        side_effects=True,
    )
    assert await ejecutar_tool(tool, {}, _contexto("whatsapp")) == {"real": True}


def test_numero_simulado_no_se_confunde_con_el_real() -> None:
    assert numeracion("PRUEBA", 1) == "PRUEBA-0001"
    assert numeracion("PRUEBA", 0) == "PRUEBA-0000"
    assert is_test_channel("test") and not is_test_channel("whatsapp")


def test_solo_el_panel_usa_el_sandbox() -> None:
    import types

    evals = types.SimpleNamespace(channel="test", state={"is_test": True})
    panel = types.SimpleNamespace(channel="test", state={"is_test": True, "panel": True})
    real = types.SimpleNamespace(channel="whatsapp", state={"panel": True})
    assert is_panel_test(panel) is True
    assert is_panel_test(evals) is False
    assert is_panel_test(real) is False


def test_todas_las_tools_declaran_lectura_o_simulador() -> None:
    """Una tool nueva sin declaración no puede volver a escribir en producción desde el panel."""
    from pack_calendar.skills import CalendarSkill
    from pack_cobranza.skills import CobranzaSkill
    from pack_commerce.skills import CatalogSkill, LeadSkill, OrderSkill

    from core.datos_cliente.aviso_tool import AvisoDatosSkill
    from core.datos_cliente.tools import DatosClienteSkill
    from core.memory.historial_tool import HistorialClienteSkill
    try:
        from pack_knowledge.skills import KnowledgeSkill
    except ImportError:  # el pack es opcional en algunos entornos
        KnowledgeSkill = None  # type: ignore[assignment,misc]

    from core.subscriptions.tools import SubscriptionSkill
    from core.tools.core_skill import CoreSkill

    skills = [
        CoreSkill(),
        DatosClienteSkill(),
        AvisoDatosSkill(),
        HistorialClienteSkill(),
        SubscriptionSkill(),
        LeadSkill(),
        CatalogSkill(),
        OrderSkill(),
        CalendarSkill(),
        CobranzaSkill(),
        *([KnowledgeSkill()] if KnowledgeSkill is not None else []),
    ]
    faltan: list[str] = []
    sin_simulador: list[str] = []
    for skill in skills:
        for tool in skill.get_tools():
            if not tool.solo_lectura and not tool.side_effects:
                faltan.append(tool.name)
            if tool.side_effects and not tool.permite_en_prueba and tool.simular is None:
                sin_simulador.append(tool.name)
    assert faltan == [], f"tools sin declarar solo_lectura/side_effects: {faltan}"
    assert sin_simulador == [], f"tools con efectos sin simulador: {sin_simulador}"
