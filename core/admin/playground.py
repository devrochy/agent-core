"""Playground: probar el asistente de un tenant sin pasar por ningún canal.

Llama al MISMO loop del runtime (`core.orchestrator.loop.process_conversation`)
sobre una conversación sintética marcada con `channel == TEST_CHANNEL`. El loop
GENERA y DEVUELVE la respuesta (`TurnResult`); acá se devuelve al navegador --
nunca se llama a `send_to_conversation`, así que no toca Meta ni waweb.

Historial: vive en working memory (Redis) igual que una conversación real. Además, como
el consumidor y el envío, se guardan en `messages` el mensaje de la prueba y la respuesta.
Las tools con efectos se simulan (HU-ARCH-14) y al cerrar o reiniciar la prueba se purga
la conversación, el cliente sintético y todo lo que colgó de ellos. Mientras dura, queda
fuera de las métricas, las listas y el escalamiento (`core/shared/test_mode.py`).

Aislamiento: cada operación corre bajo el `tenant_scope` del admin
(middleware del panel); todas las queries filtran `tenant_id` explícito además
de RLS (invariante #1).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select, text

from core.db.models import Conversation, Customer, Message
from core.db.session import get_session
from core.gateway.conexion_pruebas import asegurar_conexion_de_pruebas, validar_conexion_para_probar
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
    # HU-ARCH-14: alguna tool se ejecutó en modo simulado (no se creó data real).
    simulado: bool = False


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


async def conexion_real_para_probar(session: Any, tenant_id: uuid.UUID) -> uuid.UUID | None:
    """La conexión conversacional del negocio con release activa para el Centro de pruebas: la
    principal primero, luego la más antigua. None si no hay ninguna."""
    from sqlalchemy import text

    fila = (
        await session.execute(
            text(
                "select c.id from channel_connections c join connection_types t on t.id = c.type_id "
                "where c.tenant_id = :t and c.deleted_at is null and c.active_release_id is not null "
                "and t.produces_conversations and c.type_id <> 'playground' "
                "order by c.is_primary desc, c.created_at limit 1"
            ),
            {"t": tenant_id},
        )
    ).first()
    return fila[0] if fila else None


async def get_or_create_test_conversation(
    tenant_id: uuid.UUID, connection_id: uuid.UUID | None = None
) -> Conversation:
    """Conversación de prueba "viva" del tenant (una sola a la vez por conexión).

    Sin `connection_id`, un negocio v3 prueba por su conexión real con release activa
    (`conexion_real_para_probar`, ADR-0191); si no tiene ninguna, por la interna del playground.
    Con `connection_id` (HU-AOS-57) se ata a esa conexión del negocio y el turno corre por su
    release activa, es decir, por el agente publicado ahí; sigue siendo del canal `test`."""
    async with get_session() as session:
        customer = await _get_or_create_customer(session, tenant_id)
        # El contract del Agent OS vuelve `connection_id` obligatorio: la conversación de prueba
        # también va por una conexión (la interna del negocio, o la elegida si es válida).
        if connection_id is None:
            # Un negocio v3 prueba por su conexión real con release (ADR-0191): la interna no tiene
            # release y su turno fallaría cerrado. Sin ninguna, queda la interna (y el turno no responde).
            connection_id = await conexion_real_para_probar(session, tenant_id)
        if connection_id is not None:
            conexion_id = await validar_conexion_para_probar(session, tenant_id, connection_id)
        else:
            conexion_id = await asegurar_conexion_de_pruebas(session, tenant_id)
        filtros = [
            Conversation.tenant_id == tenant_id,
            Conversation.customer_id == customer.id,
            Conversation.channel == TEST_CHANNEL,
            Conversation.status != "closed",
            Conversation.deleted_at.is_(None),
        ]
        if connection_id is not None:
            filtros.append(Conversation.connection_id == conexion_id)
        else:
            # La de siempre: la interna (o una de antes sin conexión); nunca una atada a un agente.
            filtros.append(or_(Conversation.connection_id.is_(None), Conversation.connection_id == conexion_id))
        conversation = (
            await session.execute(select(Conversation).where(*filtros).order_by(Conversation.started_at.desc()))
        ).scalars().first()
        if conversation is None:
            conversation = Conversation(
                tenant_id=tenant_id,
                customer_id=customer.id,
                channel=TEST_CHANNEL,
                status="open",
                state={"is_test": True, "panel": True},
                connection_id=conexion_id,
            )
            session.add(conversation)
            await session.flush()
        else:
            estado = dict(conversation.state or {})
            if not estado.get("panel"):
                estado["panel"] = True
                conversation.state = estado
            if conversation.connection_id is None:
                conversation.connection_id = conexion_id
            await session.flush()
        return conversation


async def list_test_turns(conversation_id: uuid.UUID) -> list[dict]:
    """Historial de la prueba (working memory). `[]` si Redis no responde."""
    from core.memory import working as working_memory

    return await working_memory.get_recent_turns(conversation_id)


async def close_test_conversation(tenant_id: uuid.UUID, connection_id: uuid.UUID | None = None) -> int:
    """Purga la prueba (conversación, cliente sintético, mensajes, tareas, ledger y memoria).

    Sin `connection_id` cierra todas las pruebas vivas del negocio; con él, solo la de esa
    conexión. La purga es física y acotada a `channel = 'test'` (HU-ARCH-14): al cerrar la
    prueba no queda data. Devuelve cuántas conversaciones cerró.
    """
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            filtros = [
                Conversation.tenant_id == tenant_id,
                Conversation.channel == TEST_CHANNEL,
                Conversation.state["panel"].astext == "true",
            ]
            if connection_id is not None:
                filtros.append(Conversation.connection_id == connection_id)
            ids = list(
                (
                    await session.execute(select(Conversation.id).where(*filtros))
                ).scalars().all()
            )
        await _purga_test(tenant_id, ids)
    return len(ids)


async def _purga_test(tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    """Purga las conversaciones indicadas y limpia Redis/sandbox. El llamador ya está en scope."""
    from core.mcp.sandbox import SandboxPrueba
    from core.memory import working as working_memory

    if not ids:
        return
    async with get_session() as session:
        for conversation_id in ids:
            await session.execute(
                text("select purge_test_conversation(:t, :c)"),
                {"t": str(tenant_id), "c": str(conversation_id)},
            )
    for conversation_id in ids:
        await working_memory.clear(conversation_id)
        await SandboxPrueba(tenant_id, conversation_id).limpiar()


async def reset_test_conversation(tenant_id: uuid.UUID, connection_id: uuid.UUID | None = None) -> Conversation:
    """Purga la prueba actual y arranca una nueva desde cero."""
    # Primero la resuelve/marca: una conversación de antes del marcador `panel` también se purga.
    await get_or_create_test_conversation(tenant_id, connection_id)
    await close_test_conversation(tenant_id, connection_id)
    return await get_or_create_test_conversation(tenant_id, connection_id)


async def run_test_turn(tenant_id: uuid.UUID, text: str, connection_id: uuid.UUID | None = None) -> TestTurn:
    """Procesa un turno contra el loop real y devuelve la respuesta (sin canal)."""
    from core.orchestrator.loop import process_conversation

    conversation = await get_or_create_test_conversation(tenant_id, connection_id)
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
    return TestTurn(
        text=result.text,
        image_url=result.image_url,
        escalated=result.escalated,
        simulado=getattr(result, "simulado", False),
    )


async def _salientes(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> list[Message]:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Message)
                .where(
                    Message.tenant_id == tenant_id,
                    Message.conversation_id == conversation_id,
                    Message.direction == "outbound",
                )
                .order_by(Message.created_at, Message.id)
            )
        ).scalars().all()
        session.expunge_all()
        return list(rows)


async def enviar_opciones(
    tenant_id: uuid.UUID, actor: str, clave: str, connection_id: uuid.UUID | None = None
) -> tuple[dict, list[Message]]:
    """Envía la plantilla con opciones `clave` a la conversación de prueba (SPEC-40, ADR-0172, pista B1) y devuelve
    el resultado del envío y los mensajes que quedaron guardados, listos para pintar. Los errores de negocio son los
    de `plantillas_opciones_service.enviar_prueba` (`ErrorPlantillaOpciones`). Corre bajo el `tenant_scope` del admin."""
    from core.admin import plantillas_opciones_service as svc
    from core.gateway.opciones_envio import PREFIJO_ID

    conversation = await get_or_create_test_conversation(tenant_id, connection_id)
    # Cada envío del playground es nuevo: la clave de idempotencia solo existe para que `enviar_prueba` la exija.
    r = await svc.enviar_prueba(tenant_id, actor, clave, conversation.id, uuid.uuid4().hex)
    ids = {f"{PREFIJO_ID}:{r['send_id']}", f"{PREFIJO_ID}:{r['send_id']}:0"}
    return r, [m for m in await _salientes(tenant_id, conversation.id) if m.client_message_id in ids]


async def tocar_opcion(
    tenant_id: uuid.UUID, payload: str, connection_id: uuid.UUID | None = None
) -> tuple[str, list[Message]]:
    """Simula el toque de un botón de la conversación de prueba con el MISMO resolver que atiende WhatsApp
    (`opciones_toque.atender`: firma por negocio y conversación, vigencia, una sola ejecución; sin LLM). Devuelve el
    `outcome` y los mensajes salientes que el toque produjo. **Ejecuta la tool de verdad.** Quien llama ya comprobó
    la bandera y que el payload es de opciones (`es_de_opciones`)."""
    from core.orchestrator import opciones_toque

    conv = await get_or_create_test_conversation(tenant_id, connection_id)
    async with get_session() as session:
        conversation = await session.get(Conversation, conv.id)
        customer = await session.get(Customer, conversation.customer_id) if conversation else None
        session.expunge_all()
    if (
        conversation is None
        or customer is None
        or conversation.tenant_id != tenant_id
        or customer.tenant_id != tenant_id
    ):
        raise PlaygroundError("No existe la conversación de prueba.")
    antes = {m.id for m in await _salientes(tenant_id, conversation.id)}
    outcome = await opciones_toque.atender(conversation, customer, {"interactive_id": payload})
    return outcome, [m for m in await _salientes(tenant_id, conversation.id) if m.id not in antes]


async def _guardar_mensaje(tenant_id: uuid.UUID, conversation_id: uuid.UUID, direccion: str, texto: str) -> None:
    """Como `consumer._persist_inbound` (entrante) y el envío (saliente, ya entregado: el
    playground responde en el navegador)."""
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
