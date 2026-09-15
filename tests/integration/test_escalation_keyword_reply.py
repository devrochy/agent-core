"""`ESCALATION_REPLY` vía playbook (ROADMAP, mapa de parametrización, ítem 4):
el escalamiento por keyword en `core/orchestrator/consumer.py` es el único
call site fuera de `loop.py` -- ahí no había `AgentSpec` cargado, así que es
el punto con más riesgo de haber quedado mal enchufado. Este test confirma
que el texto REALMENTE entregado al cliente viene de
`core/playbooks/respuestas.md` (vía `load_response`), no de la constante
Python directamente.

`tests/test_core_tools.py` ya cubre `load_response("escalation")` en sí
(puro, sin BD). Este archivo verifica la parte que ese test no puede: que
`consumer.py` REALMENTE resuelve el spec del tenant en curso y entrega ese
texto por el canal (outbound persistido, vía `send_to_conversation`).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from conftest import PHONE, TEST_ALPHA_ID, _meta_payload
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID


async def _run_background(seconds: float) -> None:
    from core.orchestrator.consumer import consume_loop

    consumer = asyncio.create_task(consume_loop())
    await asyncio.sleep(seconds)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass


async def _last_outbound_text(conversation_id: uuid.UUID) -> str | None:
    from core.db.models import Message
    from core.db.session import get_session

    async with get_session() as session:
        row = (
            await session.execute(
                select(Message.content)
                .where(Message.conversation_id == conversation_id, Message.direction == "outbound")
                .order_by(Message.created_at.desc())
            )
        ).scalars().first()
    return row


async def test_escalamiento_por_keyword_entrega_el_texto_del_playbook(
    app_client, tenant_setup, recorded
):
    from core.agents.responses import load_response
    from core.agents.spec import load_active_spec
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    t1, _ = tenant_setup
    recorded.clear()

    wa_id = f"wa-esc-{uuid.uuid4().hex[:6]}"
    resp = await app_client.post(
        "/webhooks/whatsapp",
        json=_meta_payload(
            PHONE, wa_id, f"wamid.ESC.{uuid.uuid4().hex[:6]}", "quiero hablar con un asesor"
        ),
    )
    assert resp.json()["enqueued"] == 1

    await _run_background(2)

    assert recorded == [], "escalar por keyword no debe llamar al LLM"

    async with tenant_scope(t1):
        async with get_session() as session:
            customer = (
                await session.execute(
                    select(Customer).where(Customer.external_id == f"whatsapp:+{wa_id}")
                )
            ).scalar_one()
            conversation = (
                await session.execute(
                    select(Conversation).where(Conversation.customer_id == customer.id)
                )
            ).scalar_one()
            assert conversation.status == "escalated"
            assert conversation.bot_paused is True

        spec = await load_active_spec(t1)
        expected_text = await load_response(spec, "escalation")
        assert expected_text is not None, "el tenant de prueba debe tener 'respuestas' allowlisteado"

        delivered = await _last_outbound_text(conversation.id)

    assert delivered == expected_text
