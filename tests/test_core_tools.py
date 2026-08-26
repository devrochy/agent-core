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
    from mod_crm_engine.config import CRMConfig

    cfg = CRMConfig()
    assert "¡Hola" in cfg.follow_up_lead_message
    assert "{total}" in cfg.follow_up_quote_message
    assert cfg.order_status_visible["abierto"] == "en preparación"
    assert "{public_order_number}" in cfg.order_confirmed_message
    assert cfg.currency_code == "COP"