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
from dataclasses import replace
from typing import Any, ClassVar

from sqlalchemy import func, select

from core.agents.playbooks import load_playbook
from core.agents.spec import load_active_spec
from core.db.models import Conversation, UsageLedger
from core.db.session import get_session
from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.memory import entity as entity_memory
from core.memory import perfil as perfil_cliente
from core.memory import vector as vector_memory
from core.memory.facts import HechoInvalido, NamespaceNoConcedido
from core.orchestrator.escalation import escalate
from core.orchestrator.motivos_escalacion import MOTIVOS_TOOL, motivo_de_tool


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
        # Código cerrado (HU-UX-17): un `reason` fuera del catálogo queda como `otro` y su texto
        # pasa al detalle, que se guarda junto al resumen (no hay columna aparte).
        codigo, detalle = motivo_de_tool(str(args.get("reason") or ""))
        detalle = str(args.get("detalle") or "").strip() or detalle
        summary = str(args.get("summary", "")).strip()[:500]
        if detalle:
            summary = "\n\n".join(p for p in (summary, f"Detalle del motivo: {detalle[:200]}") if p)
        await escalate(ctx.conversation_id, reason=f"tool:{codigo}", summary=summary)
        return {"status": "escalated", "bot_paused": True}

    async def _get_customer_profile(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        profile = await entity_memory.get_profile(ctx.customer_id)
        normalizado = perfil_cliente.leer_perfil(
            profile,
            telefono=profile.get("contact_phone") or profile.get("phone"),
            nombre_cliente=profile.get("name"),
        )
        return {"profile": normalizado}

    async def _update_customer_profile(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        # Las tools viejas y los playbooks usan `full_name`, `contact_phone`, `cedula`…
        # Se guardan con la clave canónica del perfil para que la ficha los muestre igual.
        attributes = {
            perfil_cliente.normalizar_clave(str(k)): v
            for k, v in dict(args.get("attributes") or {}).items()
        }
        if not attributes:
            return {"error": "attributes vacío: no hay nada que actualizar"}
        try:
            await entity_memory.upsert_attributes(
                ctx.customer_id, attributes, source="agent", concedidos=ctx.write_namespaces
            )
        except NamespaceNoConcedido:
            return {"error": "este agente no puede guardar datos en el perfil del cliente"}
        except HechoInvalido:
            return {
                "error": "dato no admitido en el perfil: claves cortas de una línea, sin datos de pago, "
                "salud ni contraseñas"
            }
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
            kind="fact",
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

        Delega en `queue_outbound_email` (mismo camino que la confirmación de citas
        de HU-CAL-02): resuelve Customer/Conversation, persiste el `Message` y lo
        encola. La tool solo llega al modelo si hay una conexión email activa
        (`requires_connection_types`).
        """
        from core.gateway.outbound_email import EmailOutboxError, queue_outbound_email

        to = str(args.get("to", "")).strip()
        body = str(args.get("body", "")).strip()
        subject = str(args.get("subject", "") or "").strip()
        try:
            message_id = await queue_outbound_email(to, subject, body)
        except EmailOutboxError as exc:
            return {
                "no_connection": "No hay una conexión de correo configurada para este negocio.",
                "invalid_recipient": "email destino inválido",
                "empty_body": "body vacío",
            }.get(exc.reason, f"No se pudo enviar el correo ({exc.reason}).")
        return {"status": "queued", "to": to, "message_id": message_id}

    # ------------------------------------------------------------ definición
    def get_tools(self) -> list[MCPTool]:
        """Tools del core: siempre disponibles, fuera del flujo (HU-FWK-01, ADR-0109)."""
        return [replace(t, always_available=True) for t in self._build_tools()]

    def _build_tools(self) -> list[MCPTool]:
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
                        "reason": {
                            "type": "string",
                            "enum": list(MOTIVOS_TOOL),
                            "description": "Motivo de la escalación",
                        },
                        "detalle": {
                            "type": "string",
                            "description": "Detalle del motivo, sobre todo si es 'otro'",
                        },
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
    """Registra las skills del core en el MCPHost (idempotente)."""
    registradas = {name for name, _ in getattr(host, "_skills", {}).items()}
    if registradas >= {"core", "subscriptions"}:
        return
    host.begin_activation("core")
    try:
        if "core" not in registradas:
            host.register_skill(CoreSkill())
        if "subscriptions" not in registradas:
            from core.subscriptions.tools import SubscriptionSkill

            host.register_skill(SubscriptionSkill())
    finally:
        host.end_activation()


def register_core_skills_lazy() -> None:
    from core.mcp.host import mcp_host

    register_core_skills(mcp_host)