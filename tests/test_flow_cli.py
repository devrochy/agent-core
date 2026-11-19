"""`agent-core flow validate`: las `sub_*` del core cuentan como disponibles si el
negocio tiene un proveedor de suscripciones habilitado (HU-SUB-01), igual que en el
runtime. Antes daba falso negativo para `solvershub` (visto en test el 2026-10-08)."""

from __future__ import annotations

from agentcore_contracts import AgentSpec

from cli.commands.flow import _available_tool_names


def _spec(packs: list[str]) -> AgentSpec:
    return AgentSpec(
        id="agent-1",
        version="1.0.0",
        tenantId="t1",
        persona={
            "role": "asistente", "tone": "cercano", "warmth": 3, "emojis": "sparse",
            "maxLength": 500, "forbiddenPhrases": [],
        },
        model={"primary": "deepseek-flash", "temperature": 0.3},
        memory={"shortTermTurns": 30},
        playbooks=[], tools=[], policies={"maxToolCallsPerTurn": 10},
        packs=packs,
    )


def test_sub_tools_con_proveedor_habilitado() -> None:
    nombres = _available_tool_names(_spec(["pack_subscriptions"]), frozenset({"pack_subscriptions"}))
    assert {"sub_consultar", "sub_link_pago", "sub_cambiar_plan"} <= nombres
    assert "escalate_to_human" in nombres


def test_sin_proveedor_no_hay_sub_tools() -> None:
    nombres = _available_tool_names(_spec(["pack_commerce"]), frozenset({"pack_subscriptions"}))
    assert not {n for n in nombres if n.startswith("sub_")}
