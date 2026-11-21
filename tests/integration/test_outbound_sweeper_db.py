"""Barrido de salientes atascadas (H3) contra Postgres real.

Lo que los tests unitarios no ven: la consulta corre bajo RLS forzada en `messages` (sin
`tenant_scope` no vería filas, incidente 2026-10-05) y DISTINCT ON devuelve solo la cabeza de cada
conversación, nunca las filas recientes ni las de otro negocio.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _negocio_con_salientes(minutos: list[int]) -> tuple[uuid.UUID, list[uuid.UUID]]:
    """Un negocio con una conversación y outbound `queued` creados hace `minutos` cada uno."""
    from core.alta_negocio import onboard_tenant
    from core.db.models import Conversation, Customer, Message, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("barrido")
    tenant_id, _ = await onboard_tenant(
        name=f"Barrido {slug}", slug=slug, business_description="Barrido de salida.", persona={"tone": "cercano"}
    )
    ids: list[uuid.UUID] = []
    async with tenant_scope(tenant_id), get_session() as session:
        customer = Customer(
            tenant_id=tenant_id, channel="whatsapp", external_id=f"whatsapp:+57{uuid.uuid4().int % 10**10}",
            name="Cliente barrido",
        )
        session.add(customer)
        await session.flush()
        conversation = Conversation(tenant_id=tenant_id, customer_id=customer.id, channel="whatsapp", status="open")
        session.add(conversation)
        await session.flush()
        ahora = utcnow()
        for m in minutos:
            msg = Message(
                tenant_id=tenant_id, conversation_id=conversation.id, direction="outbound", sender_type="agent",
                content=f"hace {m} min", status="queued", created_at=ahora - timedelta(minutes=m),
                client_message_id=str(uuid.uuid4()),
            )
            session.add(msg)
            await session.flush()
            ids.append(msg.id)
    return tenant_id, ids


async def test_solo_la_cabeza_vieja_de_cada_conversacion_y_del_propio_negocio() -> None:
    from core.db.models import utcnow
    from core.tasks.outbound_sweeper import _cabezas_atascadas

    tenant_id, (cabeza, _siguiente) = await _negocio_con_salientes([40, 30])
    otro_tenant, _ = await _negocio_con_salientes([50])
    _reciente_tenant, _ = await _negocio_con_salientes([1])

    limite = utcnow() - timedelta(minutes=10)
    filas = await _cabezas_atascadas(tenant_id, limite)
    assert [f[0] for f in filas] == [cabeza]

    assert await _cabezas_atascadas(_reciente_tenant, limite) == []
    assert len(await _cabezas_atascadas(otro_tenant, limite)) == 1
