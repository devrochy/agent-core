"""agent-core#674 · el playbook `identidad` le dice al modelo su nombre y que lo diga al preguntárselo.

Sin base de datos ni LLM: se arma el prompt fijo con el nombre que congela la release.
"""

from __future__ import annotations

from typing import Any

from core.llm import prompt_builder


def _spec() -> Any:
    from agentcore_contracts import AgentSpec

    return AgentSpec(
        id="agent-1",
        version="1.0.0",
        tenantId="t1",
        persona={"role": "asistente", "tone": "cercano", "warmth": 3, "emojis": "sparse", "maxLength": 500,
                 "forbiddenPhrases": []},
        model={"primary": "deepseek-flash", "temperature": 0.3},
        memory={"shortTermTurns": 30},
        playbooks=[{"name": "identidad", "description": "Identidad del asistente"}],
        tools=[],
        policies={"maxToolCallsPerTurn": 10},
        packs=[],
    )


async def test_identidad_afirma_el_nombre_y_pide_decirlo_al_preguntar() -> None:
    ctx = prompt_builder.PromptContext(assistant_name="Asistente QA 56BD96", business_description="Tienda de café")
    (bloque,) = await prompt_builder.bloques_fijos(_spec(), ctx, tenant_slug="")
    assert "Eres Asistente QA 56BD96, el asistente virtual de: Tienda de café" in bloque
    assert "Tu nombre es Asistente QA 56BD96." in bloque
    assert "cómo te llamas" in bloque
    assert "{" not in bloque  # ningún placeholder quedó sin sustituir


async def test_otro_nombre_cambia_el_prompt() -> None:
    ctx_a = prompt_builder.PromptContext(assistant_name="Ana", business_description="Tienda")
    ctx_b = prompt_builder.PromptContext(assistant_name="Beto", business_description="Tienda")
    (a,) = await prompt_builder.bloques_fijos(_spec(), ctx_a, tenant_slug="")
    (b,) = await prompt_builder.bloques_fijos(_spec(), ctx_b, tenant_slug="")
    assert "Ana" in a and "Beto" not in a
    assert "Beto" in b and "Ana" not in b
