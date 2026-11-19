"""Una ráfaga del mismo cliente queda en UNA conversación, aunque se procese en paralelo.

El consumidor crea una tarea por entrada del batch: tres mensajes seguidos de un cliente
sin conversación abierta corren `_resolve_conversation` a la vez. Sin candado, cada tarea
veía "no hay conversación" y creaba la suya, y los mensajes terminaban en turnos distintos
(visto en `test_sprint1::test_tres_mensajes_en_2s_un_turno`: `['msg 1', 'msg 0\\nmsg 2']`).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from conftest import PHONE, TEST_ALPHA_ID
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RAFAGA = 5


async def test_una_rafaga_concurrente_crea_una_sola_conversacion(tenant_setup, monkeypatch) -> None:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async def cuota_lenta(*_a, **_k) -> None:
        # Solo se llama en la rama "no existe", entre el select y el insert: abre a
        # propósito la ventana en la que las tareas de la ráfaga se pisan.
        await asyncio.sleep(0.2)

    monkeypatch.setattr(consumer, "enforce_metric", cuota_lenta)
    fields = {
        "channel": "whatsapp",
        "customer_external_id": f"wa-rafaga-{uuid.uuid4().hex[:8]}",
        "connection_external_id": PHONE,
    }

    async with tenant_scope(TEST_ALPHA_ID):
        # El cliente ya existe y no tiene conversación abierta (la cerró un turno anterior):
        # con un cliente nuevo, el índice único de `customers` serializa la ráfaga por su
        # cuenta y la carrera de la conversación no se vería.
        async with get_session() as session:
            session.add(
                Customer(tenant_id=TEST_ALPHA_ID, channel="whatsapp", external_id=fields["customer_external_id"])
            )

        resultados = await asyncio.gather(*[consumer._resolve_conversation(fields) for _ in range(RAFAGA)])

        assert len({conversation.id for conversation, _, _ in resultados}) == 1
        async with get_session() as session:
            abiertas = (
                await session.execute(
                    select(func.count())
                    .select_from(Conversation)
                    .join(Customer, Customer.id == Conversation.customer_id)
                    .where(
                        Conversation.tenant_id == TEST_ALPHA_ID,
                        Customer.external_id == fields["customer_external_id"],
                        Conversation.status.in_(["open", "escalated"]),
                    )
                )
            ).scalar_one()
    assert abiertas == 1
