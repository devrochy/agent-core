"""Tools nativas del core y playbook de respuestas (Sprint 2, Fase B).

Unit tests sin BD: registro de la skill del core y extracción de mensajes.
"""

from __future__ import annotations

import pytest
from agentcore_contracts import AgentSpec

from core.agents.responses import load_response
from core.tools.core_skill import CoreSkill

EXPECTED_TOOLS = {
    "escalate_to_human",
    "get_customer_profile",
    "update_customer_profile",
    "remember_fact",
    "search_memories",
    "load_playbook",
    "get_conversation_usage",
    "set_conversation_note",
}


def make_spec(playbooks: list[dict[str, str]]) -> AgentSpec:
    return AgentSpec(
        id="agent-1",
        version="1.0.0",
        tenantId="t1",
        persona={
            "role": "asistente",
            "tone": "cercano",
            "warmth": 3,
            "emojis": "sparse",
            "maxLength": 500,
            "forbiddenPhrases": [],
            "locale": "es-CO",
        },
        model={"primary": "deepseek-chat", "temperature": 0.3},
        memory={"shortTermTurns": 30, "summarize": True},
        playbooks=playbooks,
        tools=[],
        policies={
            "escalation": {"triggers": []},
            "confirmBeforeWrite": True,
            "maxToolCallsPerTurn": 10,
        },
        packs=[],
    )


def test_core_skill_registra_ocho_tools() -> None:
    skill = CoreSkill()
    tools = skill.get_tools()
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS
    # descripciones de producto: no vacías, con cuándo usar / cuándo NO
    for tool in tools:
        assert tool.description.strip()
        assert tool.parameters.get("type") == "object"


def test_core_skill_siempre_activa() -> None:
    assert CoreSkill.keywords == []


@pytest.mark.asyncio
async def test_load_response_extrae_seccion() -> None:
    spec = make_spec(
        [
            {"name": "identidad", "description": "I"},
            {"name": "respuestas", "description": "Respuestas"},
        ]
    )
    msg = await load_response(spec, "budget_exhausted")
    assert msg is not None
    assert "presupuesto" in msg.lower()

    unknown = await load_response(spec, "no-existe")
    assert unknown is None


def test_crm_config_copy_editable_por_tenant() -> None:
    """ADR-0017: el copy de CULTO vive en CRMConfig (config), no en el código."""
    from pack_commerce.config import CRMConfig

    cfg = CRMConfig()
    assert "¡Hola" in cfg.follow_up_lead_message


@pytest.mark.asyncio
async def test_tool_denial_pack_y_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR-0019: la costura de ejecución niega tools de packs no habilitados y
    tools deshabilitadas por ToolBinding. Fail-open si no hay binding."""
    from agentcore_contracts.contracts import ToolBinding

    from core.mcp.types import MCPTool
    from core.orchestrator.loop import _tool_denial

    spec = make_spec([])
    assert "pack_fake" not in spec.packs

    fake_tool = MCPTool(
        name="fake_pack_tool",
        description="t",
        parameters={"type": "object", "properties": {}},
        handler=lambda *a, **k: {"ok": True},
        plugin_id="pack_fake",
    )
    import core.orchestrator.loop as loop_mod

    monkeypatch.setattr(
        loop_mod.mcp_host, "get_tool", lambda name: fake_tool if name == fake_tool.name else None
    )

    # pack no habilitado → se niega en la costura de ejecución (defensa en profundidad)
    assert _tool_denial(spec, "fake_pack_tool") is not None

    # pack habilitado → corre
    spec.packs = ["core", "pack_fake"]
    assert _tool_denial(spec, "fake_pack_tool") is None

    # binding deshabilitado → niega aunque el pack esté habilitado
    spec.tools = [ToolBinding(tool="fake_pack_tool", enabled=False, scopes=[])]
    assert "deshabilitada" in _tool_denial(spec, "fake_pack_tool")

    # fail-open: tool del core sin binding corre
    spec.tools = []
    core_tool = MCPTool(
        name="core_fake",
        description="t",
        parameters={},
        handler=lambda *a, **k: {"ok": True},
        plugin_id="core",
    )
    monkeypatch.setattr(
        loop_mod.mcp_host, "get_tool", lambda name: core_tool if name == "core_fake" else None
    )
    assert _tool_denial(spec, "core_fake") is None


def test_crm_config_copy_mensajes_pedido() -> None:
    """ADR-0017: mensajes de pedido de CULTO en CRMConfig (config), no en código."""
    from pack_commerce.config import CRMConfig

    cfg = CRMConfig()
    assert "{total}" in cfg.follow_up_quote_message
    assert cfg.order_status_visible["abierto"] == "en preparación"
    assert "{public_order_number}" in cfg.order_confirmed_message
    assert cfg.currency_code == "COP"


def test_calendar_skill_sin_estado_por_tenant() -> None:
    """C4 #2: el skill de calendar NO captura la config de un solo tenant en
    activación (eso era una fuga cruzada: tokens del tenant B en el calendario
    del A). La config se resuelve por invocación desde el tenant actual."""
    from pack_calendar.skills import CalendarSkill

    skill = CalendarSkill()
    assert not hasattr(skill, "_config"), "config debe resolverse por invocación"
    assert hasattr(skill, "_tenant_config")
    tools = skill.get_tools()
    assert {t.name for t in tools} == {"create_calendar_event", "check_availability"}