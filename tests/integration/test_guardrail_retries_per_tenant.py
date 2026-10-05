"""`guardrail_max_retries` por tenant -- parametrización pedida explícitamente
por el usuario ("intentos fallidos" configurable, no fijo para todos los
tenants). Mismo patrón exacto que `debounce_mode`
(`AppSettings.behavior_config`, sin tocar el contrato `AgentSpec` congelado
-- eso exigiría un ADR nuevo, invariante 4 de `CLAUDE.md`).

Antes de este cambio, `MAX_GUARDRAIL_RETRIES = 1` (`core/orchestrator/
guardrails.py:25`) era una constante global: TODOS los tenants tenían
exactamente 1 reintento antes de escalar, sin poder ajustarlo. Ahora
`loop.py` resuelve `guardrail_max_retries` desde `behavior_config` con
fallback a ese mismo default.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from conftest import PHONE, TEST_ALPHA_ID, _meta_payload, _meta_request
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID


async def _set_behavior(tenant_id: uuid.UUID, **overrides) -> None:
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = (
                await session.execute(select(AppSettings).where(AppSettings.tenant_id == tenant_id))
            ).scalar_one_or_none()
            if row is None:
                row = AppSettings(tenant_id=tenant_id, assistant_name="it")
                session.add(row)
            behavior = dict(row.behavior_config or {})
            behavior.update(overrides)
            row.behavior_config = behavior


async def _ensure_max_tool_calls_per_turn(tid: uuid.UUID, value: int) -> None:
    """Algún otro test de la suite (`test_tools_limits.py`) deja
    `maxToolCallsPerTurn=1` persistido para T1 en la BD local (no se resetea
    entre corridas) -- con guardrail_max_retries>0 eso corta el turno por
    "max_iterations" antes de completar los reintentos esperados. Se fija
    explícito acá para no depender de qué dejó una corrida anterior."""
    import copy

    from core.db.models import AgentSpecRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        from core.agents.spec import ensure_default_spec

        await ensure_default_spec(tid)
        async with get_session() as session:
            row = (
                await session.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == tid,
                        AgentSpecRecord.is_active.is_(True),
                    )
                )
            ).scalar_one()
            spec = copy.deepcopy(row.spec)
            spec.setdefault("policies", {})["maxToolCallsPerTurn"] = value
            row.spec = spec


async def _run_background(seconds: float) -> None:
    from core.orchestrator.consumer import consume_loop

    consumer = asyncio.create_task(consume_loop())
    await asyncio.sleep(seconds)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass


class _AlwaysFailingGuardrail:
    """Guardrail de prueba: falla siempre, para forzar la ruta de reintento."""

    async def __call__(self, response_text: str, tool_results: list):
        from core.mcp.types import GuardrailResult

        return GuardrailResult(passed=False, reason="test_siempre_falla", retry_instruction="[test]")


@pytest.fixture
def _registered_failing_guardrail():
    from core.mcp.host import mcp_host

    guardrail = _AlwaysFailingGuardrail()
    mcp_host.begin_activation("core")
    mcp_host.register_guardrail("test_siempre_falla", guardrail, priority=1)
    mcp_host.end_activation()
    try:
        yield
    finally:
        mcp_host._guardrails = [g for g in mcp_host._guardrails if g.name != "test_siempre_falla"]


async def test_guardrail_max_retries_0_escala_en_la_primera_llamada(
    app_client, tenant_setup, recorded, _registered_failing_guardrail
):
    t1, _ = tenant_setup
    await _ensure_max_tool_calls_per_turn(t1, 10)
    await _set_behavior(t1, debounce_mode="inmediato", guardrail_max_retries=0, disabled_guardrails=[])
    recorded.clear()

    body, headers = _meta_request(_meta_payload(PHONE, f"wa-grmax0-{uuid.uuid4().hex[:6]}", f"wamid.GRMAX0.{uuid.uuid4().hex[:6]}", "hola"))
    resp = await app_client.post(
        "/webhooks/whatsapp",
        content=body,
        headers=headers,
    )
    assert resp.json()["enqueued"] == 1
    await _run_background(2)

    assert len(recorded) == 1, (
        f"con guardrail_max_retries=0 no debería haber reintento, hubo {len(recorded)} llamadas al LLM"
    )


async def test_guardrail_max_retries_2_reintenta_dos_veces(
    app_client, tenant_setup, recorded, _registered_failing_guardrail
):
    t1, _ = tenant_setup
    await _ensure_max_tool_calls_per_turn(t1, 10)
    await _set_behavior(t1, debounce_mode="inmediato", guardrail_max_retries=2, disabled_guardrails=[])
    recorded.clear()

    body, headers = _meta_request(_meta_payload(PHONE, f"wa-grmax2-{uuid.uuid4().hex[:6]}", f"wamid.GRMAX2.{uuid.uuid4().hex[:6]}", "hola"))
    resp = await app_client.post(
        "/webhooks/whatsapp",
        content=body,
        headers=headers,
    )
    assert resp.json()["enqueued"] == 1
    await _run_background(2)

    assert len(recorded) == 3, (
        f"con guardrail_max_retries=2 deberían ser 3 llamadas al LLM (1 + 2 reintentos), "
        f"hubo {len(recorded)}"
    )


async def test_guardrail_individual_desactivado_no_reintenta(
    app_client, tenant_setup, recorded, _registered_failing_guardrail
):
    """Guardrail individual on/off por tenant: con
    `disabled_guardrails=["test_siempre_falla"]`, ese guardrail puntual no
    corre en absoluto -- ni un solo reintento, aunque
    `guardrail_max_retries` sea > 0 (nunca llega a evaluarse)."""
    t1, _ = tenant_setup
    await _ensure_max_tool_calls_per_turn(t1, 10)
    try:
        await _set_behavior(
            t1, debounce_mode="inmediato", guardrail_max_retries=2, disabled_guardrails=["test_siempre_falla"]
        )
        recorded.clear()

        body, headers = _meta_request(_meta_payload(PHONE, f"wa-grdis-{uuid.uuid4().hex[:6]}", f"wamid.GRDIS.{uuid.uuid4().hex[:6]}", "hola"))
        resp = await app_client.post(
            "/webhooks/whatsapp",
            content=body,
            headers=headers,
        )
        assert resp.json()["enqueued"] == 1
        await _run_background(2)

        assert len(recorded) == 1, (
            f"con el guardrail desactivado no debería haber reintento, hubo {len(recorded)} llamadas al LLM"
        )
    finally:
        # `_set_behavior` hace merge (no reemplaza) sobre una fila
        # compartida de `AppSettings` -- sin este `finally`, un assert
        # fallido deja `disabled_guardrails` pegado para T1 en la BD real
        # y contamina corridas futuras de la suite (hallazgo real de
        # `auditor-multitenant` sobre este mismo test).
        await _set_behavior(t1, disabled_guardrails=[])


# ---------------------------------------------------------------------------
# HU-CAL-04 / ADR-0092: qué pasa cuando se agotan los reintentos del guardrail.
# `behavior_config.guardrail_fail_action`: "escalate" (default) | "neutral".


async def _set_escalation(tenant_id: uuid.UUID, **overrides) -> None:
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = (
                await session.execute(select(AppSettings).where(AppSettings.tenant_id == tenant_id))
            ).scalar_one()
            row.escalation_config = {**dict(row.escalation_config or {}), **overrides}


async def _send_and_load(app_client, tenant_id: uuid.UUID, tag: str) -> dict:
    """Manda un inbound, corre el consumer y devuelve el estado resultante."""
    from core.db.models import AuditLog, Conversation, Customer, Escalation, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    wa_id = f"wa-{tag}-{uuid.uuid4().hex[:6]}"
    body, headers = _meta_request(
        _meta_payload(PHONE, wa_id, f"wamid.{tag.upper()}.{uuid.uuid4().hex[:6]}", "hola")
    )
    resp = await app_client.post("/webhooks/whatsapp", content=body, headers=headers)
    assert resp.json()["enqueued"] == 1
    await _run_background(2)

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            customer = (
                await session.execute(select(Customer).where(Customer.external_id == f"whatsapp:+{wa_id}"))
            ).scalar_one()
            conversation = (
                await session.execute(select(Conversation).where(Conversation.customer_id == customer.id))
            ).scalar_one()
            escalations = (
                await session.execute(select(Escalation).where(Escalation.conversation_id == conversation.id))
            ).scalars().all()
            audits = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.conversation_id == conversation.id,
                        AuditLog.action == "guardrail_neutral",
                    )
                )
            ).scalars().all()
            outbound = "\n\n".join(
                (
                    await session.execute(
                        select(Message.content)
                        .where(Message.conversation_id == conversation.id, Message.direction == "outbound")
                        .order_by(Message.created_at)
                    )
                ).scalars().all()
            )
    return {
        "conversation": conversation,
        "escalations": escalations,
        "audits": audits,
        "outbound": outbound,
    }


async def test_guardrail_neutral_no_escala_y_responde_verifying(
    app_client, tenant_setup, recorded, _registered_failing_guardrail
):
    from core.orchestrator.escalation import VERIFYING_REPLY

    t1, _ = tenant_setup
    await _ensure_max_tool_calls_per_turn(t1, 10)
    try:
        await _set_behavior(
            t1,
            debounce_mode="inmediato",
            guardrail_max_retries=1,
            guardrail_fail_action="neutral",
            disabled_guardrails=[],
            split_style="un_mensaje",
        )
        await _set_escalation(t1, auto_escalate_after_turns=0)
        recorded.clear()

        state = await _send_and_load(app_client, t1, "grneu")

        assert len(recorded) == 2, "1 intento + 1 reintento antes de la salida neutral"
        assert state["escalations"] == [], "con 'neutral' no debe crearse una escalación"
        assert state["conversation"].bot_paused is False
        assert state["conversation"].status != "escalated"
        assert state["outbound"] == VERIFYING_REPLY
        assert len(state["audits"]) == 1
        assert state["audits"][0].input == {"guardrail": "test_siempre_falla"}
        assert state["conversation"].state.get("turnos_sin_resolver") == 1
    finally:
        await _set_behavior(t1, guardrail_fail_action="escalate", guardrail_max_retries=1)
        await _set_escalation(t1, auto_escalate_after_turns=0)


async def test_guardrail_sin_accion_configurada_escala_como_siempre(
    app_client, tenant_setup, recorded, _registered_failing_guardrail
):
    t1, _ = tenant_setup
    await _ensure_max_tool_calls_per_turn(t1, 10)
    await _set_behavior(
        t1, debounce_mode="inmediato", guardrail_max_retries=0, guardrail_fail_action=None,
        disabled_guardrails=[],
    )
    recorded.clear()

    state = await _send_and_load(app_client, t1, "gresc")

    assert [e.reason for e in state["escalations"]] == ["guardrail"]
    assert state["conversation"].bot_paused is True
    assert state["audits"] == []


async def test_guardrail_neutral_respeta_el_autoescalado_por_turnos(
    app_client, tenant_setup, recorded, _registered_failing_guardrail
):
    """La salida neutral cuenta como turno sin resolver: con el umbral en 1,
    el autoescalado (ADR-0051) sigue siendo la red de seguridad."""
    from core.orchestrator.escalation import VERIFYING_REPLY

    t1, _ = tenant_setup
    await _ensure_max_tool_calls_per_turn(t1, 10)
    try:
        await _set_behavior(
            t1, debounce_mode="inmediato", guardrail_max_retries=0, guardrail_fail_action="neutral",
            disabled_guardrails=[], split_style="un_mensaje",
        )
        await _set_escalation(t1, auto_escalate_after_turns=1)
        recorded.clear()

        state = await _send_and_load(app_client, t1, "grauto")

        assert [e.reason for e in state["escalations"]] == ["turns_unresolved"]
        assert state["conversation"].bot_paused is True
        assert state["outbound"].startswith(VERIFYING_REPLY)
    finally:
        await _set_behavior(t1, guardrail_fail_action="escalate")
        await _set_escalation(t1, auto_escalate_after_turns=0)
