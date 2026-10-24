"""Playground: probar el asistente de un tenant sin pasar por ningún canal.

Llama al MISMO loop del runtime (`core.orchestrator.loop.process_conversation`)
sobre una conversación sintética marcada con `channel == TEST_CHANNEL`. El loop
GENERA y DEVUELVE la respuesta (`TurnResult`); acá se devuelve al navegador --
nunca se llama a `send_to_conversation`, así que no toca Meta ni waweb.

Historial: vive en working memory (Redis) igual que una conversación real. Además, como
el consumidor y el envío, se guardan en `messages` el mensaje de la prueba y la respuesta:
lo que depende de ellos (la aceptación de los términos del alta, ADR-0125) tiene que poder
probarse desde el panel. Son de la conversación `test`, que queda fuera de las métricas
de cuota (también `mensajes`), de las listas y del escalamiento (`core/shared/test_mode.py`).

Aislamiento: cada operación corre bajo el `tenant_scope` del admin
(middleware del panel); todas las queries filtran `tenant_id` explícito además
de RLS (invariante #1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from core.db.models import Conversation, Customer, utcnow
from core.db.session import get_session
from core.gateway.conexion_pruebas import asegurar_conexion_de_pruebas
from core.shared.logger import get_logger
from core.shared.test_mode import TEST_CHANNEL

log = get_logger(__name__)

TEST_EXTERNAL_ID = "panel-playground"
TEST_CUSTOMER_NAME = "Prueba (panel)"


class PlaygroundError(Exception):
    """Error de negocio del playground (sin AgentSpec, turno no procesable)."""


@dataclass
class TestTurn:
    text: str
    image_url: str | None = None
    escalated: bool = False


async def _get_or_create_customer(session, tenant_id: uuid.UUID) -> Customer:
    customer = (
        await session.execute(
            select(Customer).where(
                Customer.tenant_id == tenant_id,
                Customer.channel == TEST_CHANNEL,
                Customer.external_id == TEST_EXTERNAL_ID,
                Customer.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if customer is None:
        customer = Customer(
            tenant_id=tenant_id,
            channel=TEST_CHANNEL,
            external_id=TEST_EXTERNAL_ID,
            name=TEST_CUSTOMER_NAME,
        )
        session.add(customer)
        await session.flush()
    return customer


async def get_or_create_test_conversation(tenant_id: uuid.UUID) -> Conversation:
    """Conversación de prueba "viva" del tenant (una sola a la vez)."""
    async with get_session() as session:
        customer = await _get_or_create_customer(session, tenant_id)
        conversation = (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.customer_id == customer.id,
                    Conversation.channel == TEST_CHANNEL,
                    Conversation.status != "closed",
                    Conversation.deleted_at.is_(None),
                )
                .order_by(Conversation.started_at.desc())
            )
        ).scalars().first()
        # El contract del Agent OS vuelve `connection_id` obligatorio: la conversación de prueba
        # también va por la conexión interna del negocio (la liga si es una de antes).
        conexion_id = await asegurar_conexion_de_pruebas(session, tenant_id)
        if conversation is None:
            conversation = Conversation(
                tenant_id=tenant_id,
                customer_id=customer.id,
                channel=TEST_CHANNEL,
                status="open",
                state={"is_test": True},
                connection_id=conexion_id,
            )
            session.add(conversation)
            await session.flush()
        elif conversation.connection_id is None:
            conversation.connection_id = conexion_id
            await session.flush()
        return conversation


async def list_test_turns(conversation_id: uuid.UUID) -> list[dict]:
    """Historial de la prueba (working memory). `[]` si Redis no responde."""
    from core.memory import working as working_memory

    return await working_memory.get_recent_turns(conversation_id)


async def reset_test_conversation(tenant_id: uuid.UUID) -> Conversation:
    """Cierra la prueba actual y arranca una nueva desde cero."""
    from core.memory import working as working_memory

    current = await get_or_create_test_conversation(tenant_id)
    async with get_session() as session:
        row = await session.get(Conversation, current.id)
        if row is not None and row.tenant_id == tenant_id:
            row.status = "closed"
            row.closed_at = utcnow()
    await working_memory.clear(current.id)
    return await get_or_create_test_conversation(tenant_id)


async def run_test_turn(tenant_id: uuid.UUID, text: str) -> TestTurn:
    """Procesa un turno contra el loop real y devuelve la respuesta (sin canal)."""
    from core.orchestrator.loop import process_conversation

    conversation = await get_or_create_test_conversation(tenant_id)
    await _guardar_mensaje(tenant_id, conversation.id, "inbound", text)
    try:
        result = await process_conversation(conversation.id, text)
    except ValueError as exc:
        # `load_active_spec` levanta ValueError("Sin AgentSpec para el tenant").
        raise PlaygroundError(str(exc)) from exc
    if result is None:
        raise PlaygroundError(
            "No se pudo procesar el turno. Verificá que el tenant tenga un AgentSpec "
            "activo y que el bot no esté pausado."
        )
    if result.text:
        await _guardar_mensaje(tenant_id, conversation.id, "outbound", result.text)
    return TestTurn(text=result.text, image_url=result.image_url, escalated=result.escalated)


async def _guardar_mensaje(tenant_id: uuid.UUID, conversation_id: uuid.UUID, direccion: str, texto: str) -> None:
    """Como `consumer._persist_inbound` (entrante) y el envío (saliente, ya entregado: el
    playground responde en el navegador)."""
    from core.db.models import Message

    async with get_session() as session:
        session.add(
            Message(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                direction=direccion,
                sender_type="customer" if direccion == "inbound" else "agent",
                content=texto,
                status="sent",
            )
        )
