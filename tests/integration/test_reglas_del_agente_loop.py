"""HU-AOS-97 · el turno aplica cada regla del agente con su acción (ADR-0051 §3, ADR-0206 §1).

`retry` pide un reintento correctivo; `escalate` no gasta reintentos y va directo a la acción de falla del negocio.
Stack real y un LLM falso (sin red).
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from test_verificador_loop import _conversacion, _Guion, _negocio, _preparar, _turno

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

CON_DESCUENTO = "Te hago un descuento especial si compras hoy."
SIN_DESCUENTO = "Con gusto te ayudo con tu pedido."


def _con_reglas(monkeypatch: pytest.MonkeyPatch, reglas: list[dict[str, Any]]) -> None:
    """El agente del turno de la release, con estas reglas (como si el dueño las hubiera publicado)."""
    import dataclasses

    from agentcore_contracts import AgentSpecV3

    import core.orchestrator.loop as loop

    original = loop.del_turno

    def del_turno() -> Any:
        agente = original()
        if agente is None:
            return None
        contenido = agente.v3.model_dump(mode="json", by_alias=True) | {"guardrails": reglas}
        return dataclasses.replace(agente, v3=AgentSpecV3.model_validate(contenido))

    async def sin_compilado() -> None:
        return None

    # Sin el compilado por versión el loop arma el agente con `del_turno` (su respaldo), que aquí lleva las reglas.
    monkeypatch.setattr("core.orchestrator.loop._compilado_del_turno", sin_compilado)
    monkeypatch.setattr("core.orchestrator.loop.del_turno", del_turno)


def _regla(accion: str) -> dict[str, Any]:
    return {"name": "sin_descuentos", "kind": "forbidden_phrase", "phrases": ["descuento"], "action": accion}


async def test_regla_retry_reintenta_y_entrega_la_corregida(monkeypatch: pytest.MonkeyPatch) -> None:
    tid = await _negocio(guardrail_max_retries=1)
    conversation_id = await _conversacion(tid)
    guion = _Guion([CON_DESCUENTO, SIN_DESCUENTO])
    _preparar(monkeypatch, guion)
    _con_reglas(monkeypatch, [_regla("retry")])

    result = await _turno(tid, conversation_id)

    assert result is not None and not result.escalated
    assert result.text == SIN_DESCUENTO
    assert guion.llamadas == 2 and len(guion.instrucciones) == 1


async def test_regla_escalate_escala_sin_reintento(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import select

    from core.db.models import Escalation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio(guardrail_max_retries=1)
    conversation_id = await _conversacion(tid)
    guion = _Guion([CON_DESCUENTO, SIN_DESCUENTO])
    _preparar(monkeypatch, guion)
    _con_reglas(monkeypatch, [_regla("escalate")])

    result = await _turno(tid, conversation_id)

    assert result is not None and result.escalated
    assert guion.llamadas == 1 and guion.instrucciones == []
    async with tenant_scope(tid):
        async with get_session() as session:
            escalaciones = (
                (await session.execute(select(Escalation).where(Escalation.conversation_id == conversation_id)))
                .scalars()
                .all()
            )
    assert [(e.reason, e.summary) for e in escalaciones] == [("guardrail", "regla")]
