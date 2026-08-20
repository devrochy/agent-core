"""Skills del CRM: LeadSkill (captura/calificación) y DealSkill (oportunidades).

Demuestra el contrato completo:
- Dos skills en un plugin (agrupación semántica).
- Idempotencia con context.idempotency_key.
- Actualización de la Entity Memory (Capa 2) del core.
- El LLM nunca ve conversation_id/customer_id: van en el ToolContext.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from core.db.session import get_session
from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.memory import entity as entity_memory
from mod_crm_engine.models import Deal, Lead


class LeadSkill(BaseSkill):
    name = "lead_skill"
    description = "Captura y calificación de leads (prospectos de venta)"
    keywords = []  # siempre activa: capturar leads es el corazón del CRM

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="crear_lead",
                description=(
                    "Registra un nuevo prospecto (lead) en el CRM. Úsala cuando el cliente "
                    "muestre interés y ya tengas al menos su nombre. NO la uses dos veces "
                    "para la misma persona: si ya existe, usa actualizar_lead."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Nombre completo del prospecto"},
                        "email": {"type": "string", "description": "Email si lo dio"},
                        "interest": {"type": "string", "description": "Qué producto/servicio le interesa"},
                    },
                    "required": ["name", "interest"],
                },
                handler=self.crear_lead,
            ),
            MCPTool(
                name="calificar_lead",
                description=(
                    "Asigna una puntuación 1-10 al lead según presupuesto, urgencia y poder de "
                    "decisión. Úsala después de hacer al menos 2 preguntas de calificación."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "score": {"type": "integer", "description": "Puntuación 1-10"},
                        "notes": {"type": "string", "description": "Justificación breve"},
                    },
                    "required": ["score"],
                },
                handler=self.calificar_lead,
            ),
            MCPTool(
                name="actualizar_lead",
                description="Actualiza datos del lead existente de esta conversación (email, interés, estado).",
                parameters={
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "interest": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["nuevo", "calificado", "contactado", "convertido", "perdido"],
                        },
                    },
                },
                handler=self.actualizar_lead,
            ),
        ]

    # ------------------------------------------------------------ handlers
    async def crear_lead(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        async with get_session() as session:
            existing = (
                await session.execute(
                    select(Lead).where(Lead.idempotency_key == context.idempotency_key)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return {"lead_id": str(existing.id), "status": "duplicate"}

            by_customer = (
                await session.execute(select(Lead).where(Lead.customer_id == context.customer_id))
            ).scalar_one_or_none()
            if by_customer is not None:
                return {
                    "lead_id": str(by_customer.id),
                    "status": "ya_existe",
                    "hint": "Este cliente ya es un lead. Usa actualizar_lead.",
                }

            lead = Lead(
                customer_id=context.customer_id,
                conversation_id=context.conversation_id,
                name=arguments.get("name"),
                email=arguments.get("email"),
                interest=arguments.get("interest", ""),
                idempotency_key=context.idempotency_key,
            )
            session.add(lead)
            await session.flush()
            lead_id = str(lead.id)

        # Entity Memory (Capa 2): el core recuerda esto en TODAS las conversaciones futuras
        await entity_memory.upsert_attributes(
            context.customer_id,
            {"name": arguments.get("name"), "email": arguments.get("email"), "crm_lead_id": lead_id},
        )
        return {"lead_id": lead_id, "status": "created"}

    async def calificar_lead(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        score = max(1, min(10, int(arguments.get("score", 0))))
        async with get_session() as session:
            lead = (
                await session.execute(select(Lead).where(Lead.customer_id == context.customer_id))
            ).scalar_one_or_none()
            if lead is None:
                return {"error": "No hay lead para esta conversación. Usa crear_lead primero."}
            lead.score = score
            lead.status = "calificado"
            lead.last_activity_at = datetime.now(timezone.utc)
        return {"lead_id": str(lead.id), "score": score, "status": "calificado"}

    async def actualizar_lead(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        async with get_session() as session:
            lead = (
                await session.execute(select(Lead).where(Lead.customer_id == context.customer_id))
            ).scalar_one_or_none()
            if lead is None:
                return {"error": "No hay lead para esta conversación. Usa crear_lead primero."}
            if arguments.get("email"):
                lead.email = str(arguments["email"])
            if arguments.get("interest"):
                lead.interest = str(arguments["interest"])
            if arguments.get("status"):
                lead.status = str(arguments["status"])
            lead.last_activity_at = datetime.now(timezone.utc)
        if arguments.get("email"):
            await entity_memory.upsert_attributes(context.customer_id, {"email": arguments["email"]})
        return {"lead_id": str(lead.id), "status": "actualizado"}


class DealSkill(BaseSkill):
    name = "deal_skill"
    description = "Oportunidades de venta (deals) asociadas a un lead"
    keywords = ["cotizar", "cotización", "presupuesto", "propuesta", "precio", "comprar", "negocio"]

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="crear_oportunidad",
                description=(
                    "Crea una oportunidad de venta (deal) para el lead de esta conversación. "
                    "Úsala cuando el cliente pida una propuesta o cotización concreta."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Título del deal, ej: 'Desarrollo web'"},
                        "amount": {"type": "number", "description": "Monto estimado (0 si no se sabe)"},
                        "notes": {"type": "string"},
                    },
                    "required": ["title"],
                },
                handler=self.crear_oportunidad,
            ),
            MCPTool(
                name="actualizar_etapa",
                description="Mueve el deal de etapa: propuesta -> negociacion -> ganado | perdido.",
                parameters={
                    "type": "object",
                    "properties": {
                        "stage": {
                            "type": "string",
                            "enum": ["propuesta", "negociacion", "ganado", "perdido"],
                        },
                    },
                    "required": ["stage"],
                },
                handler=self.actualizar_etapa,
            ),
        ]

    async def crear_oportunidad(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        async with get_session() as session:
            existing = (
                await session.execute(
                    select(Deal).where(Deal.idempotency_key == context.idempotency_key)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return {"deal_id": str(existing.id), "status": "duplicate"}

            lead = (
                await session.execute(select(Lead).where(Lead.customer_id == context.customer_id))
            ).scalar_one_or_none()
            deal = Deal(
                lead_id=lead.id if lead else None,
                title=str(arguments.get("title", "")),
                amount=float(arguments.get("amount", 0) or 0),
                notes=str(arguments.get("notes", "")),
                idempotency_key=context.idempotency_key,
            )
            session.add(deal)
            await session.flush()
            deal_id = str(deal.id)
        return {"deal_id": deal_id, "status": "created", "stage": "propuesta"}

    async def actualizar_etapa(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        async with get_session() as session:
            lead = (
                await session.execute(select(Lead).where(Lead.customer_id == context.customer_id))
            ).scalar_one_or_none()
            if lead is None:
                return {"error": "No hay lead ni deal para esta conversación."}
            deal = (
                await session.execute(
                    select(Deal).where(Deal.lead_id == lead.id).order_by(Deal.created_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
            if deal is None:
                return {"error": "No hay deal. Usa crear_oportunidad primero."}
            deal.stage = str(arguments["stage"])
            if deal.stage == "ganado":
                lead.status = "convertido"
        return {"deal_id": str(deal.id), "stage": deal.stage}
