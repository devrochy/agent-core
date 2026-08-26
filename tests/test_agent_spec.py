"""Tests del AgentSpec desde BD y playbooks (Sprint 2, ADR-0014).

Unit tests sin BD: el ensamblador y el loader reciben un `AgentSpec` construido,
no tocan la base de datos.
"""

from __future__ import annotations

import pytest
from agentcore_contracts import AgentSpec

from core.agents.playbooks import load_playbook
from core.llm.prompt_builder import PromptContext, build_system_blocks

DEFAULT_PLAYBOOKS = [
    {"name": "identidad", "description": "Identidad del asistente"},
    {"name": "tono", "description": "Voz y tono del asistente"},
    {"name": "reglas-operacion", "description": "Reglas de operación del asistente"},
]


def make_spec(playbooks: list[dict[str, str]] | None = None) -> AgentSpec:
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
        playbooks=playbooks if playbooks is not None else DEFAULT_PLAYBOOKS,
        tools=[],
        policies={
            "escalation": {"triggers": []},
            "confirmBeforeWrite": True,
            "maxToolCallsPerTurn": 10,
        },
        packs=[],
    )


@pytest.mark.asyncio
async def test_ensamblador_usa_playbooks_y_placeholders() -> None:
    spec = make_spec()
    ctx = PromptContext(
        assistant_name="Mi Asistente",
        business_description="Tienda X",
        language="es",
    )
    blocks = await build_system_blocks(spec, ctx)

    assert any("Eres Mi Asistente, el asistente virtual de: Tienda X" in b for b in blocks)
    assert any("## Tono" in b for b in blocks)
    assert any("## Reglas de operación" in b for b in blocks)
    # El bloque de reglas traduce el language a español
    assert any("Responde SIEMPRE en español" in b for b in blocks)
    # El frontmatter de los playbooks no debe filtrarse al prompt
    assert not any(b.startswith("---") for b in blocks)


@pytest.mark.asyncio
async def test_load_playbook_respeta_allowlist() -> None:
    spec = make_spec(playbooks=[{"name": "tono", "description": "Tono"}])
    content = await load_playbook(spec, "tono")
    assert content is not None and "Tono" in content

    # fuera del índice del tenant: rechazado
    outside = await load_playbook(spec, "identidad")
    assert outside is None


@pytest.mark.asyncio
async def test_load_playbook_quita_frontmatter() -> None:
    spec = make_spec()
    content = await load_playbook(spec, "identidad")
    assert content is not None
    assert not content.lstrip().startswith("---")
    assert "Eres" in content


@pytest.mark.asyncio
async def test_ensamblador_respeta_orden_del_indice() -> None:
    spec = make_spec(
        playbooks=[
            {"name": "reglas-operacion", "description": "Reglas"},
            {"name": "identidad", "description": "Identidad"},
        ]
    )
    ctx = PromptContext(assistant_name="A", business_description="B")
    blocks = await build_system_blocks(spec, ctx)
    assert "## Reglas de operación" in blocks[0]
    assert "Eres A" in blocks[1]