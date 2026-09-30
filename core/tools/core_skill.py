"""Tools NATIVAS del core (Sprint 2).

Plomería del framework, no lógica de negocio: memoria (capas 2 y 3),
escalamiento, playbooks, presupuesto y notas. Se registran como una skill del
core (`plugin_id="core"`): una falla aquí NUNCA desactiva el core (el auto-disable
salta para el id "core", ver loop._execute_tool_safely).

Principios (adaptados de /new-tool): descripciones de producto (máx 3 líneas:
qué hace / cuándo usarla / cuándo NO), parámetros JSON Schema, errores como
resultado tipado (no se lanzan al runtime).
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from sqlalchemy import func, select

from core.agents.playbooks import load_playbook
from core.agents.spec import load_active_spec
from core.db.models import Conversation, Customer, Message, UsageLedger
from core.db.session import get_session
from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.memory import entity as entity_memory
from core.memory import vector as vector_memory
from core.orchestrator.escalation import escalate
from core.shared.tenant import get_current_tenant_id


async def conversation_spent(conversation_id: uuid.UUID) -> float:
    """Costo acumulado de la conversación según usage_ledger (RLS lo acota)."""
    async with get_session() as session:
        total = (
            await session.execute(
                select(func.coalesce(func.sum(UsageLedger.cost_usd), 0)).where(
                    UsageLedger.conversation_id == conversation_id
                )
            )
        ).scalar()
        return float(total or 0)


class CoreSkill(BaseSkill):
    name = "core"
    description = "Herramientas del núcleo: memoria del cliente, escalamiento, playbooks y presupuesto."
    keywords: ClassVar[list[str]] = []  # siempre activa

    # ------------------------------------------------------------ handlers
    async def _escalate_to_human(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        reason = str(args.get("reason", "manual"))[:80]
        summary = str(args.get("summary", ""))[:500]
        await escalate(ctx.conversation_id, reason=f"tool:{reason}", summary=summary)
        return {"status": "escalated", "bot_paused": True}

    async def _get_customer_profile(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        profile = await entity_memory.get_profile(ctx.customer_id)
        return {"profile": profile}

    async def _update_customer_profile(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        attributes = dict(args.get("attributes") or {})
        if not attributes:
            return {"error": "attributes vacío: no hay nada que actualizar"}
        await entity_memory.upsert_attributes(ctx.customer_id, attributes)
        return {"status": "ok", "updated": list(attributes.keys())}

    async def _remember_fact(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        content = str(args.get("content", "")).strip()
        if not content:
            return {"error": "content vacío"}
        await vector_memory.store_memory(
            content=content,
            customer_id=ctx.customer_id,
            conversation_id=ctx.conversation_id,
            meta={"source": "core:remember_fact"},
        )
        return {"status": "stored"}

    async def _search_memories(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            return {"error": "query vacío"}
        top_k = int(args.get("top_k", 3) or 3)
        results = await vector_memory.recall(
            query, customer_id=ctx.customer_id, conversation_id=ctx.conversation_id, top_k=top_k
        )
        return {"memories": results}

    async def _load_playbook(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()
        if not name:
            return {"error": "name vacío"}
        spec = await load_active_spec()
        content = await load_playbook(spec, name)
        if content is None:
            return {"error": f"playbook '{name}' no disponible para este tenant"}
        return {"content": content}

    async def _get_conversation_usage(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        spec = await load_active_spec()
        spent = await conversation_spent(ctx.conversation_id)
        budget = spec.policies.budgetUsdPerConversation
        return {
            "spent_usd": round(spent, 6),
            "budget_usd": budget,
            "remaining_usd": round(budget - spent, 6) if budget is not None else None,
        }

    async def _set_conversation_note(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        note = str(args.get("note", "")).strip()
        if not note:
            return {"error": "note vacío"}
        async with get_session() as session:
            conversation = await session.get(Conversation, ctx.conversation_id)
            if conversation is None:
                return {"error": "conversación no encontrada"}
            state = dict(conversation.state)
            notes = list(state.get("notes", []))
            notes.append(note)
            state["notes"] = notes
            conversation.state = state
        return {"status": "saved", "note_count": len(notes)}

    async def _send_email(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Encola un correo saliente por el canal de correo del negocio (ADR-0071).

        Reutiliza la cola/reintentos de `outbound_send`: crea el `Customer` y la
        `Conversation` del canal email si no existen, persiste el `Message` y lo
        encola. La tool solo llega al modelo si hay una conexión email activa
        (`requires_connection_types`).
        """
        to = str(args.get("to", "")).strip()
        from core.gateway.channels.email import single_recipient

        recipient = single_recipient(to)
        if recipient is None:
            return {"error": "email destino inválido"}
        to = recipient
        body = str(args.get("body", "")).strip()
        if not body:
            return {"error": "body vacío"}
        subject = str(args.get("subject", "") or "").strip()
        if any(c in subject for c in "\r\n"):
            return {"error": "asunto inválido"}

        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            return {"error": "sin contexto de tenant"}

        from core.gateway.connections import get_active_connection

        connection = await get_active_connection("email_smtp")
        if connection is None:
            return {"error": "No hay una conexión de correo configurada para este negocio."}

        async with get_session() as session:
            customer = (
                await session.execute(
                    select(Customer).where(
                        Customer.tenant_id == tenant_id,
                        Customer.channel == "email",
                        Customer.external_id == to,
                        Customer.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if customer is None:
                customer = Customer(tenant_id=tenant_id, channel="email", external_id=to)
                session.add(customer)
                await session.flush()

            conversation = (
                await session.execute(
                    select(Conversation)
                    .where(
                        Conversation.tenant_id == tenant_id,
                        Conversation.customer_id == customer.id,
                        Conversation.connection_id == connection.id,
                        Conversation.status == "open",
                        Conversation.deleted_at.is_(None),
                    )
                    .order_by(Conversation.started_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if conversation is None:
                conversation = Conversation(
                    tenant_id=tenant_id,
                    customer_id=customer.id,
                    channel="email",
                    connection_id=connection.id,
                    status="open",
                )
                session.add(conversation)
                await session.flush()

            content = f"Subject: {subject or '(sin asunto)'}\n\n{body}"
            message = Message(
                tenant_id=tenant_id,
                conversation_id=conversation.id,
                direction="outbound",
                sender_type="agent",
                content=content,
                status="queued",
                client_message_id=str(uuid.uuid4()),
            )
            session.add(message)
            await session.flush()
            message_id = message.id

        from core.tasks.outbound_send import outbound_send_task

        outbound_send_task.delay(str(message_id), str(tenant_id))
        return {"status": "queued", "to": to, "message_id": str(message_id)}

    # ------------------------------------------------------------ definición
    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="escalate_to_human",
                description=(
                    "Transfiere la conversación a un asesor humano y PAUSA el bot. "
                    "Úsala cuando el cliente lo pida, insista o el caso lo amerite. "
                    "NO la uses para temas que el agente puede resolver."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "description": "Motivo de la escalación"},
                        "summary": {"type": "string", "description": "Resumen para el asesor"},
                    },
                    "required": ["reason"],
                },
                handler=self._escalate_to_human,
            ),
            MCPTool(
                name="get_customer_profile",
                description=(
                    "Devuelve el perfil estructurado del cliente (nombre, contacto y "
                    "atributos guardados). Úsala antes de personalizar. Es de solo lectura."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self._get_customer_profile,
            ),
            MCPTool(
                name="update_customer_profile",
                description=(
                    "Guarda atributos del cliente (preferencias, datos de contacto, IDs). "
                    "Úsala tras obtener un dato nuevo. NO guardes datos bancarios ni secretos."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "attributes": {
                            "type": "object",
                            "description": "Mapa clave/valor a fusionar en el perfil",
                        }
                    },
                    "required": ["attributes"],
                },
                handler=self._update_customer_profile,
            ),
            MCPTool(
                name="remember_fact",
                description=(
                    "Guarda un hecho relevante de la conversación en memoria de largo plazo. "
                    "Úsala para datos que el cliente mencionó y conviene recordar. "
                    "NO la uses para información trivial o sensible."
                ),
                parameters={
                    "type": "object",
                    "properties": {"content": {"type": "string", "description": "El hecho a recordar"}},
                    "required": ["content"],
                },
                handler=self._remember_fact,
            ),
            MCPTool(
                name="search_memories",
                description=(
                    "Busca recuerdos relevantes del cliente. Úsala para recordar qué "
                    "dijo antes. NO la uses para buscar catálogos o inventario."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Tema a recuperar"},
                        "top_k": {"type": "integer", "description": "Máximo de resultados (default 3)"},
                    },
                    "required": ["query"],
                },
                handler=self._search_memories,
            ),
            MCPTool(
                name="load_playbook",
                description=(
                    "Carga el contenido de un playbook (procedimiento) del tenant. "
                    "Úsala cuando el cliente pida algo que sigue un proceso definido. "
                    "El nombre debe estar en el índice de playbooks del agente."
                ),
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "Nombre del playbook"}},
                    "required": ["name"],
                },
                handler=self._load_playbook,
            ),
            MCPTool(
                name="get_conversation_usage",
                description=(
                    "Devuelve el costo acumulado y el presupuesto de esta conversación. "
                    "Úsala solo si el cliente pregunta por el costo. Es de solo lectura."
                ),
                parameters={"type": "object", "properties": {}, "required": []},
                handler=self._get_conversation_usage,
            ),
            MCPTool(
                name="set_conversation_note",
                description=(
                    "Guarda una nota breve en la conversación (para un humano). "
                    "Úsala al escalar o al dejar contexto relevante."
                ),
                parameters={
                    "type": "object",
                    "properties": {"note": {"type": "string", "description": "La nota"}},
                    "required": ["note"],
                },
                handler=self._set_conversation_note,
            ),
            MCPTool(
                name="send_email",
                description=(
                    "Envía un correo al cliente desde la cuenta del negocio (confirmaciones, "
                    "datos acordados). Úsala solo si el cliente dio su email. "
                    "NO la uses para spam ni para datos sensibles."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Email destino"},
                        "subject": {"type": "string", "description": "Asunto del correo"},
                        "body": {"type": "string", "description": "Cuerpo del correo"},
                    },
                    "required": ["to", "body"],
                },
                handler=self._send_email,
                requires_connection_types=["email_smtp"],
            ),
        ]


def register_core_skills(host) -> None:
    """Registra la skill del core en el MCPHost (idempotente)."""
    if any(name == "core" for name, _ in getattr(host, "_skills", {}).items()):
        return
    host.begin_activation("core")
    try:
        host.register_skill(CoreSkill())
    finally:
        host.end_activation()


def register_core_skills_lazy() -> None:
    from core.mcp.host import mcp_host

    register_core_skills(mcp_host)