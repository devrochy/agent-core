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
    "send_email",
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
        },
        model={"primary": "deepseek-chat", "temperature": 0.3},
        memory={"shortTermTurns": 30},
        playbooks=playbooks,
        tools=[],
        policies={
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


@pytest.mark.asyncio
async def test_load_response_fallback_y_escalation_coinciden_con_las_constantes() -> None:
    """`FALLBACK_REPLY`/`ESCALATION_REPLY` (core/orchestrator/loop.py,
    core/orchestrator/escalation.py) se movieron al playbook `respuestas` --
    esto fija que fue una relocalización de copy, no una reescritura: el
    texto del playbook debe ser IGUAL, no solo parecido, al de la constante
    que sigue existiendo en el código como fallback determinístico."""
    from core.orchestrator.escalation import ESCALATION_REPLY
    from core.orchestrator.loop import FALLBACK_REPLY

    spec = make_spec([{"name": "respuestas", "description": "Respuestas"}])

    assert await load_response(spec, "fallback") == FALLBACK_REPLY
    assert await load_response(spec, "escalation") == ESCALATION_REPLY


@pytest.mark.asyncio
async def test_load_response_ante_error_de_io_degrada_a_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FALLBACK_REPLY`/`ESCALATION_REPLY` (loop.py/consumer.py) hacen
    `await load_response(...) or CONSTANTE` como red de seguridad -- eso solo
    funciona si un error real de IO al leer el `.md` (permisos, disco,
    encoding) también degrada a `None` en vez de propagar la excepción y
    tumbar el turno (hallazgo de auditor-multitenant, ítem 4 fila 170 del
    ROADMAP)."""
    from pathlib import Path

    def _revienta(self: Path, encoding: str = "utf-8") -> str:
        raise OSError("disco caído (simulado)")

    monkeypatch.setattr(Path, "read_text", _revienta)

    spec = make_spec([{"name": "respuestas", "description": "Respuestas"}])
    assert await load_response(spec, "fallback") is None
    assert await load_response(spec, "escalation") is None


@pytest.mark.asyncio
async def test_load_response_sin_playbook_respuestas_devuelve_none() -> None:
    """Un tenant sin `respuestas` en su allowlist (o cualquier fallo de carga
    del playbook) debe devolver `None` -- es lo que el código en loop.py/
    consumer.py usa para caer al fallback determinístico `or FALLBACK_REPLY`/
    `or ESCALATION_REPLY`, no una condición solo teórica."""
    spec = make_spec([{"name": "identidad", "description": "I"}])

    assert await load_response(spec, "fallback") is None
    assert await load_response(spec, "escalation") is None


def test_crm_config_copy_editable_por_tenant() -> None:
    """ADR-0017: el copy del negocio vive en CRMConfig (config), no en el código."""
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
    assert await _tool_denial(spec, "fake_pack_tool") is not None

    # pack habilitado → corre
    spec.packs = ["core", "pack_fake"]
    assert await _tool_denial(spec, "fake_pack_tool") is None

    # binding deshabilitado → niega aunque el pack esté habilitado
    spec.tools = [ToolBinding(tool="fake_pack_tool", enabled=False, scopes=[])]
    assert "deshabilitada" in await _tool_denial(spec, "fake_pack_tool")

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
    assert await _tool_denial(spec, "core_fake") is None


def test_crm_config_copy_mensajes_pedido() -> None:
    """ADR-0017: mensajes de pedido en CRMConfig (config), no en código."""
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
    assert {t.name for t in tools} == {
        "create_calendar_event",
        "check_availability",
        "update_calendar_event",
        "cancel_calendar_event",
        "list_client_appointments",
        "list_appointment_types",
    }

@pytest.mark.asyncio
async def test_tool_binding_scopes_maxcalls_requires_confirmation(monkeypatch) -> None:
    """ADR-0050: el motor real de ToolBinding. `enabled` ya existía; acá se cubren
    maxCallsPerConversation, requiresConfirmation y que scopes no rompa."""
    import uuid as _uuid

    from agentcore_contracts.contracts import ToolBinding

    from core.mcp.types import MCPTool
    from core.orchestrator.loop import _binding_requires_confirmation, _tool_denial

    spec = make_spec([])
    spec.packs = ["core"]

    tool = MCPTool(
        name="safe_tool",
        description="t",
        parameters={},
        handler=lambda *a, **k: {"ok": True},
        plugin_id="core",
    )
    import core.orchestrator.loop as loop_mod

    monkeypatch.setattr(
        loop_mod.mcp_host, "get_tool", lambda name: tool if name == tool.name else None
    )

    # scopes presentes no bloquean por sí solos (autorización la aplica el pack/guardrail)
    spec.tools = [ToolBinding(tool="safe_tool", enabled=True, scopes=["orders:write"])]
    assert await _tool_denial(spec, "safe_tool") is None

    # requiresConfirmation se detecta
    spec.tools = [ToolBinding(tool="safe_tool", enabled=True, scopes=[], requiresConfirmation=True)]
    assert _binding_requires_confirmation(spec, "safe_tool") is True
    assert _binding_requires_confirmation(spec, "otra") is False

    # maxCallsPerConversation: con `audit_log` de 2 llamadas y tope 2 → niega
    spec.tools = [
        ToolBinding(tool="safe_tool", enabled=True, scopes=[], maxCallsPerConversation=2)
    ]
    conv_id = _uuid.uuid4()

    async def _fake_count(conversation_id, tool_name):
        return 2

    monkeypatch.setattr(loop_mod, "_tool_calls_in_conversation", _fake_count)
    denial = await _tool_denial(spec, "safe_tool", conv_id)
    assert denial is not None and "máximo" in denial


@pytest.mark.asyncio
async def test_tool_denial_requiere_conexion(monkeypatch) -> None:
    """ADR-0072: una tool que requiere una conexión activa se deniega sin ella."""
    from core.mcp.types import MCPTool
    from core.orchestrator.loop import _tool_denial

    spec = make_spec([])
    spec.packs = ["core"]
    tool = MCPTool(
        name="agendar",
        description="t",
        parameters={},
        handler=lambda *a, **k: {"ok": True},
        plugin_id="core",
        requires_connection_types=["google_calendar"],
    )
    import core.orchestrator.loop as loop_mod

    monkeypatch.setattr(
        loop_mod.mcp_host, "get_tool", lambda name: tool if name == tool.name else None
    )

    async def _sin_conexion(_tid):
        return set()

    monkeypatch.setattr("core.gateway.connections.active_connection_type_ids", _sin_conexion)
    denial = await _tool_denial(spec, "agendar")
    assert denial is not None and "google_calendar" in denial

    async def _con_conexion(_tid):
        return {"google_calendar"}

    monkeypatch.setattr("core.gateway.connections.active_connection_type_ids", _con_conexion)
    assert await _tool_denial(spec, "agendar") is None
