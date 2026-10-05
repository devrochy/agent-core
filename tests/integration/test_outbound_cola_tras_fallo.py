"""La cola de salida por conversación no se corta tras un fallo permanente.

Incidente en test (2026-10-05): un mensaje de WhatsApp falló para siempre (tope
diario de waweb), `outbound_send_task` lo marcó `failed` y llamó a
`_enqueue_next` FUERA de `tenant_scope`. Con RLS forzada en `messages`, la consulta
no veía filas, no se encolaba el siguiente y los 12 mensajes que venían detrás
quedaron en `queued` para siempre (el envío es secuencial por conversación).
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _conversacion_con_dos_salientes() -> tuple[uuid.UUID, uuid.UUID, object, uuid.UUID, uuid.UUID]:
    from core.db.models import Conversation, Customer, Message, utcnow
    from core.db.session import get_session
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = uniq("cola")
    tenant_id, _ = await onboard_tenant(
        name=f"Cola {slug}", slug=slug, business_description="Cola de salida.", persona={"tone": "cercano"}
    )
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            customer = Customer(
                tenant_id=tenant_id, channel="whatsapp", external_id=f"whatsapp:+57{uuid.uuid4().int % 10**10}",
                name="Cliente cola",
            )
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                tenant_id=tenant_id, customer_id=customer.id, channel="whatsapp", status="open"
            )
            session.add(conversation)
            await session.flush()
            base = utcnow()
            primero = Message(
                tenant_id=tenant_id, conversation_id=conversation.id, direction="outbound",
                sender_type="agent", content="primero", status="failed", created_at=base,
                client_message_id=str(uuid.uuid4()),
            )
            segundo = Message(
                tenant_id=tenant_id, conversation_id=conversation.id, direction="outbound",
                sender_type="agent", content="segundo", status="queued", created_at=base + timedelta(seconds=5),
                client_message_id=str(uuid.uuid4()),
            )
            session.add_all([primero, segundo])
            await session.flush()
            return tenant_id, conversation.id, primero.created_at, primero.id, segundo.id


async def test_tras_un_fallo_permanente_se_encola_el_siguiente(monkeypatch) -> None:
    from core.tasks import outbound_send as mod

    encolados: list[tuple[str, str]] = []
    monkeypatch.setattr(mod.outbound_send_task, "delay", lambda mid, tid: encolados.append((mid, tid)))

    tenant_id, conversation_id, created_at, primero_id, segundo_id = await _conversacion_con_dos_salientes()

    # Igual que en `outbound_send_task` tras `_mark_failed`: sin tenant_scope ambiente.
    await mod._enqueue_next(conversation_id, tenant_id, created_at, primero_id)

    assert encolados == [(str(segundo_id), str(tenant_id))], (
        "la cola se cortó: el siguiente mensaje en 'queued' no se encoló tras el fallo"
    )
