"""Cron jobs del CRM.

- follow_up_leads: reengancha leads calificados inactivos dentro de la ventana
  de 24h de WhatsApp (patrón 'cazador de ventas' de agent-sale).
- follow_up_quotes: reengancha cotizaciones en borrador sin convertir dentro de
  la ventana de 3-20h (ag-sale: ventana de mensajería proactiva).

El decorador @register_cron salta automáticamente si el plugin está inactivo.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.decorators import register_cron
from mod_crm_engine.models import Lead, Quote

log = get_logger(__name__)

FOLLOW_UP_LEAD_MESSAGE = (
    "¡Hola{name}! 👋 Quedamos pendientes de tu interés en {interest}. "
    "¿Sigues interesado? Estoy aquí para ayudarte con lo que necesites."
)

FOLLOW_UP_QUOTE_MESSAGE = (
    "¡Hola! Te tengo lista tu cotización por {total}. ¿La confirmo para armar tu pedido? 😋"
)


async def _tenant_ids() -> list[uuid.UUID]:
    """Tenants activos. Query de sistema (tenants no tiene RLS)."""
    async with get_session() as session:
        rows = (
            await session.execute(text("select id from tenants where deleted_at is null"))
        ).scalars().all()
    return [uuid.UUID(str(x)) for x in rows]


@register_cron("mod_crm_engine", "0 9 * * *")
async def follow_up_leads() -> None:
    """Reengancha leads calificados inactivos, dentro de la ventana de 24h de WhatsApp."""
    from core.db.models import Conversation
    from core.gateway.sender import send_to_conversation
    from core.plugin_manager import plugin_manager

    total_candidates = 0
    total_sent = 0
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            config = await plugin_manager.load_config("mod_crm_engine")
            follow_up_hours = getattr(config, "follow_up_hours", 24) or 24
            threshold = datetime.now(UTC) - timedelta(hours=follow_up_hours)
            # Ventana de 24h de WhatsApp: solo leads con actividad en las últimas 24h
            window_limit = datetime.now(UTC) - timedelta(hours=24)

            async with get_session() as session:
                leads = (
                    (
                        await session.execute(
                            select(Lead).where(
                                Lead.status == "calificado",
                                Lead.last_activity_at < threshold,
                                Lead.last_activity_at > window_limit,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )

            total_candidates += len(leads)
            for lead in leads:
                if lead.conversation_id is None:
                    continue
                async with get_session() as session:
                    conversation = await session.get(Conversation, lead.conversation_id)
                if conversation is None or conversation.bot_paused or conversation.status != "open":
                    continue
                message = FOLLOW_UP_LEAD_MESSAGE.format(
                    name=f" {lead.name}" if lead.name else "",
                    interest=lead.interest or "nuestros productos",
                )
                try:
                    await send_to_conversation(lead.conversation_id, message)
                    async with get_session() as session:
                        fresh = await session.get(Lead, lead.id)
                        if fresh is not None:
                            fresh.status = "contactado"
                            fresh.last_activity_at = datetime.now(UTC)
                    total_sent += 1
                except Exception:
                    log.exception("follow_up_send_failed", lead_id=str(lead.id))

    log.info("follow_up_completed", kind="lead", candidates=total_candidates, sent=total_sent)


@register_cron("mod_crm_engine", "30 9,15,20 * * *")
async def follow_up_quotes() -> None:
    """Reengancha cotizaciones en borrador de hace 3-20h (ventana proactiva).

    Una cotización se reengancha una sola vez (follow_up_sent_at) y solo si la
    conversación sigue abierta y dentro de la ventana de 24h de WhatsApp.
    """
    from core.db.models import Conversation
    from core.gateway.sender import send_to_conversation

    total_candidates = 0
    total_sent = 0
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            now = datetime.now(UTC)
            min_age = now - timedelta(hours=20)
            max_age = now - timedelta(hours=3)
            window_limit = now - timedelta(hours=24)

            async with get_session() as session:
                quotes = (
                    await session.execute(
                        select(Quote).where(
                            Quote.status.in_(("draft", "aceptada")),
                            Quote.follow_up_sent_at.is_(None),
                            Quote.created_at < min_age,
                            Quote.created_at > max_age,
                            Quote.created_at > window_limit,
                        )
                    )
                ).scalars().all()

            total_candidates += len(quotes)
            for quote in quotes:
                async with get_session() as session:
                    conversation = await session.get(Conversation, quote.conversation_id)
                if conversation is None or conversation.bot_paused or conversation.status != "open":
                    continue
                message = FOLLOW_UP_QUOTE_MESSAGE.format(total=f"${float(quote.total):,.0f}")
                try:
                    await send_to_conversation(quote.conversation_id, message)
                    async with get_session() as session:
                        fresh = await session.get(Quote, quote.id)
                        if fresh is not None:
                            fresh.follow_up_sent_at = now
                    total_sent += 1
                except Exception:
                    log.exception("follow_up_quote_send_failed", quote_id=str(quote.id))

    log.info("follow_up_completed", kind="quote", candidates=total_candidates, sent=total_sent)