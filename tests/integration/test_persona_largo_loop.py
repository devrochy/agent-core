"""`persona.maxLength` como tope blando dentro del loop (adenda de ADR-0050), con el stack
real y un LLM falso que devuelve textos largos.

Caso real (HU-SH-08, 2026-10-09): "¿qué planes tienen y cuánto cuestan?" escaló 1 de 4
veces porque el modelo listó los 4 planes y pasó de 500 caracteres dos veces seguidas.
Un exceso de largo nunca escala ni pausa: un reintento propio y, si no alcanza, se
entrega la respuesta. El reintento nunca empeora el resultado ni gasta reintentos de
veracidad (`guardrail_max_retries`, `guardrail_fail_action`).
"""

from __future__ import annotations

import copy
import os
import uuid
from typing import Any

import pytest
from test_verificador_loop import _conversacion, _Guion, _negocio, _preparar

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

TOPE = 120
LARGO = (
    "Tenemos cuatro planes: Básico por $100.000 al mes, Pro por $200.000, Negocio por "
    "$400.000 y Empresa por $600.000. Todos incluyen el asistente por WhatsApp."
)
LARGO_2 = (
    "Hay cuatro planes. Básico, Pro, Negocio y Empresa, desde $100.000 hasta $600.000 al "
    "mes, y todos traen el asistente de WhatsApp incluido."
)
CORTO = "Tenemos 4 planes, desde $100.000 hasta $600.000 al mes. ¿Te cuento alguno?"
PLANES = {"generar_cotizacion": {"planes": [{"price": p} for p in (100000, 200000, 400000, 600000)]}}


async def _ajustar_spec(tid: uuid.UUID, *, persona: dict[str, Any] | None = None, **policies: Any) -> None:
    from sqlalchemy import select

    from core.agents.spec import ensure_default_spec
    from core.db.models import AgentSpecRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await ensure_default_spec(tid)
        async with get_session() as session:
            row = (
                await session.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == tid, AgentSpecRecord.is_active.is_(True)
                    )
                )
            ).scalar_one()
            spec = copy.deepcopy(row.spec)
            spec.setdefault("persona", {}).update({"maxLength": TOPE, **(persona or {})})
            spec.setdefault("policies", {}).update({"maxToolCallsPerTurn": 10, **policies})
            row.spec = spec


async def _negocio_con_tope(*, persona: dict[str, Any] | None = None, policies: dict[str, Any] | None = None, **behavior: Any):
    tid = await _negocio(**{"guardrail_max_retries": 1, **behavior})
    await _ajustar_spec(tid, persona=persona, **(policies or {}))
    return tid, await _conversacion(tid)


async def _turno(tid: uuid.UUID, conversation_id: uuid.UUID):
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await process_conversation(conversation_id, "¿qué planes tienen y cuánto cuestan?")


async def _rastro(tid: uuid.UUID, conversation_id: uuid.UUID):
    from sqlalchemy import select

    from core.db.models import AuditLog, Escalation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            escalaciones = (
                (await session.execute(select(Escalation).where(Escalation.conversation_id == conversation_id)))
                .scalars()
                .all()
            )
            largos = (
                (
                    await session.execute(
                        select(AuditLog.input).where(
                            AuditLog.conversation_id == conversation_id,
                            AuditLog.action == "persona_max_length_excedido",
                        )
                    )
                )
                .scalars()
                .all()
            )
    return escalaciones, largos


def _es_de_largo(instruccion: str) -> bool:
    return f"({TOPE} caracteres)" in instruccion or f"{TOPE} caracteres" in instruccion


async def test_largo_dos_veces_se_entrega_sin_escalar(monkeypatch) -> None:
    """El caso medido en HU-SH-08: antes escalaba y pausaba el bot."""
    tid, conversation_id = await _negocio_con_tope()
    guion = _Guion([LARGO, LARGO_2], tools=["generar_cotizacion"])
    _preparar(monkeypatch, guion, PLANES)

    result = await _turno(tid, conversation_id)

    assert result is not None and not result.escalated
    assert result.text == LARGO_2
    assert guion.llamadas == 3, "tools + borrador + un solo reintento de largo"
    assert len(guion.instrucciones) == 1 and _es_de_largo(guion.instrucciones[0])
    escalaciones, largos = await _rastro(tid, conversation_id)
    assert escalaciones == []
    assert largos == [{"largo": len(LARGO_2), "tope": TOPE, "entregado": "reintento"}]


async def test_largo_y_luego_corto_entrega_el_corto(monkeypatch) -> None:
    tid, conversation_id = await _negocio_con_tope()
    guion = _Guion([LARGO, CORTO], tools=["generar_cotizacion"])
    _preparar(monkeypatch, guion, PLANES)

    result = await _turno(tid, conversation_id)

    assert result is not None and not result.escalated
    assert result.text == CORTO
    assert guion.llamadas == 3
    _, largos = await _rastro(tid, conversation_id)
    assert largos == []


async def test_reintento_de_largo_que_inventa_un_monto_entrega_el_largo(monkeypatch) -> None:
    """El reintento nunca empeora el resultado: el texto corto inventa un monto, así que
    se entrega el borrador largo ya verificado, sin gastar `guardrail_max_retries`."""
    tid, conversation_id = await _negocio_con_tope()
    guion = _Guion([LARGO, "El plan Básico cuesta $90.000 al mes."], tools=["generar_cotizacion"])
    _preparar(monkeypatch, guion, PLANES)

    result = await _turno(tid, conversation_id)

    assert result is not None and not result.escalated
    assert result.text == LARGO
    assert guion.llamadas == 3, "tools + borrador largo + reintento de largo; sin reintento de veracidad"
    escalaciones, largos = await _rastro(tid, conversation_id)
    assert escalaciones == []
    assert largos == [{"largo": len(LARGO), "tope": TOPE, "entregado": "guardado"}]


async def test_veracidad_primero_y_despues_largo(monkeypatch) -> None:
    """Un borrador largo con un monto falso: primero el reintento del verificador (que
    gasta `guardrail_retries`); si el corregido sigue largo, el reintento de largo."""
    tid, conversation_id = await _negocio_con_tope()
    falso = LARGO.replace("$600.000", "$650.000")
    guion = _Guion([falso, LARGO, CORTO], tools=["generar_cotizacion"])
    _preparar(monkeypatch, guion, PLANES)

    result = await _turno(tid, conversation_id)

    assert result is not None and not result.escalated
    assert result.text == CORTO
    assert guion.llamadas == 4
    assert len(guion.instrucciones) == 2
    assert not _es_de_largo(guion.instrucciones[0]), "la veracidad va antes que el largo"
    assert _es_de_largo(guion.instrucciones[1])


async def test_sin_vuelta_libre_se_entrega_sin_reintento(monkeypatch) -> None:
    """El turno gastó una vuelta en tools y `maxToolCallsPerTurn=2` no deja otra: el
    reintento caería en `max_iterations` y escalaría."""
    tid, conversation_id = await _negocio_con_tope(policies={"maxToolCallsPerTurn": 2})
    guion = _Guion([LARGO], tools=["generar_cotizacion"])
    _preparar(monkeypatch, guion, PLANES)

    result = await _turno(tid, conversation_id)

    assert result is not None and not result.escalated
    assert result.text == LARGO
    assert guion.llamadas == 2
    escalaciones, largos = await _rastro(tid, conversation_id)
    assert escalaciones == []
    assert largos == [{"largo": len(LARGO), "tope": TOPE, "entregado": "sin_reintento"}]


async def test_neutral_y_sin_reintentos_no_cambian_el_largo(monkeypatch) -> None:
    """`guardrail_max_retries=0` y `guardrail_fail_action=neutral` son de veracidad: el
    largo sigue con su reintento propio y nunca responde "déjame verificar"."""
    tid, conversation_id = await _negocio_con_tope(guardrail_max_retries=0, guardrail_fail_action="neutral")
    guion = _Guion([LARGO, LARGO_2], tools=["generar_cotizacion"])
    _preparar(monkeypatch, guion, PLANES)

    result = await _turno(tid, conversation_id)

    assert result is not None and not result.escalated
    assert result.text == LARGO_2
    assert guion.llamadas == 3


async def test_frase_prohibida_sigue_escalando(monkeypatch) -> None:
    tid, conversation_id = await _negocio_con_tope(
        persona={"forbiddenPhrases": ["garantizado"]}, guardrail_max_retries=0
    )
    _preparar(monkeypatch, _Guion(["El resultado está garantizado."]))

    result = await _turno(tid, conversation_id)

    assert result is not None and result.escalated
    escalaciones, _ = await _rastro(tid, conversation_id)
    assert [(e.reason, e.summary) for e in escalaciones] == [("guardrail", "persona_forbidden_phrase")]
