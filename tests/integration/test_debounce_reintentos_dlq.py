"""H2 (2026-11-20): lo que el reintento del debounce lee de la BD.

- La DLQ del turno diferido se arma con los campos de un entrante (conversación, cliente y
  conexión), solo del negocio del payload.
- El barrido periódico de huérfanos solo recupera inbound fuera de la ventana de un turno en
  curso, y cada mensaje una sola vez. El de arranque sigue recuperando todo.

La lógica de reintentos (contador, `TurnTimeout`, DLQ) está en `tests/test_debounce_reintentos.py`.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _conversacion(tid: uuid.UUID, *, con_conexion: bool, edad_s: int = 0) -> dict[str, uuid.UUID]:
    from core.db.models import ChannelConnection, Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    sufijo = uuid.uuid4().hex[:8]
    async with tenant_scope(tid):
        async with get_session() as session:
            connection_id = None
            if con_conexion:
                con = ChannelConnection(
                    tenant_id=tid, channel="whatsapp", provider="manual", label="h2", external_id=f"pn-h2-{sufijo}"
                )
                session.add(con)
                await session.flush()
                connection_id = con.id
            customer = Customer(tenant_id=tid, channel="whatsapp", external_id=f"wa-h2-{sufijo}", name="Juanita")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                tenant_id=tid, customer_id=customer.id, status="open", channel="whatsapp", connection_id=connection_id
            )
            session.add(conversation)
            await session.flush()
            message = Message(
                tenant_id=tid,
                conversation_id=conversation.id,
                direction="inbound",
                sender_type="customer",
                content="mensaje del cliente",
                created_at=datetime.now(UTC) - timedelta(seconds=edad_s),
            )
            session.add(message)
            await session.flush()
            return {
                "conversation": conversation.id,
                "customer": customer.id,
                "message": message.id,
                **({"connection": connection_id} if connection_id else {}),
            }


async def _borrar(tid: uuid.UUID, ids: dict[str, uuid.UUID]) -> None:
    from core.db.models import ChannelConnection, Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(Message.__table__.delete().where(Message.conversation_id == ids["conversation"]))
            await session.execute(Conversation.__table__.delete().where(Conversation.id == ids["conversation"]))
            await session.execute(Customer.__table__.delete().where(Customer.id == ids["customer"]))
            if "connection" in ids:
                await session.execute(
                    ChannelConnection.__table__.delete().where(ChannelConnection.id == ids["connection"])
                )


async def test_campos_de_la_dlq_salen_de_la_conversacion_del_negocio(tenant_setup) -> None:
    from core.orchestrator.debounce import _campos_para_dlq

    t1, t2 = tenant_setup
    ids = await _conversacion(t1, con_conexion=True)
    try:
        campos = await _campos_para_dlq(ids["conversation"], {"tenant_id": str(t1), "latest_message": "a\nb"})
        assert campos["tenant_id"] == str(t1)
        assert campos["provider"] == "manual"
        assert campos["channel"] == "whatsapp"
        assert campos["connection_id"] == str(ids["connection"])
        assert campos["connection_external_id"].startswith("pn-h2-")
        assert campos["customer_external_id"].startswith("wa-h2-")
        assert campos["customer_name"] == "Juanita"
        assert campos["content"] == "a\nb"
        assert campos["media"] == "[]"

        # Otro negocio no ve la conversación (RLS): no se arma una entrada con datos ajenos.
        with pytest.raises(LookupError):
            await _campos_para_dlq(ids["conversation"], {"tenant_id": str(t2), "latest_message": "x"})
    finally:
        await _borrar(t1, ids)


async def test_barrido_periodico_respeta_la_ventana_y_recupera_una_vez(tenant_setup) -> None:
    from core.memory.working import get_redis
    from core.orchestrator.debounce import PENDING_KEY, recover_orphaned_conversations

    t1, _ = tenant_setup
    r = get_redis()
    reciente = await _conversacion(t1, con_conexion=False)
    vieja = await _conversacion(t1, con_conexion=False, edad_s=600)
    try:
        await recover_orphaned_conversations(periodico=True)
        # El reciente puede estar en su ventana de debounce o en turno: no se toca.
        assert await r.zscore(PENDING_KEY, str(reciente["conversation"])) is None
        assert await r.zscore(PENDING_KEY, str(vieja["conversation"])) is not None

        # Su turno corre (sale del sorted set) y el mensaje sigue siendo el último: no se re-dispara.
        await r.zrem(PENDING_KEY, str(vieja["conversation"]))
        await recover_orphaned_conversations(periodico=True)
        assert await r.zscore(PENDING_KEY, str(vieja["conversation"])) is None

        # Al arrancar no hay turnos en vuelo: se recupera todo, como siempre.
        await recover_orphaned_conversations()
        assert await r.zscore(PENDING_KEY, str(reciente["conversation"])) is not None
    finally:
        await _borrar(t1, reciente)
        await _borrar(t1, vieja)
