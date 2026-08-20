"""Cron jobs del CRM.

follow_up_leads: cada día a las 9:00 (UTC) busca leads calificados sin actividad
en las últimas N horas (config del plugin) y les envía un mensaje de seguimiento
("cazador de ventas" — patrón de la Fase 12 de agent-sale).

El decorador @register_cron:
- registra la tarea en Celery Beat,
- la salta automáticamente si el plugin está inactivo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.db.session import get_session
from core.shared.logger import get_logger
from core.tasks.decorators import register_cron
from mod_crm_engine.models import Lead

log = get_logger(__name__)

FOLLOW_UP_MESSAGE = (
    "¡Hola{name}! 👋 Quedamos pendientes de tu interés en {interest}. "
    "¿Sigues interesado? Estoy aquí para ayudarte con lo que necesites."
)


@register_cron("mod_crm_engine", "0 9 * * *")
async def follow_up_leads() -> None:
    """Reengancha leads calificados inactivos, dentro de la ventana de 24h de WhatsApp."""
    from core.db.models import Conversation
    from core.gateway.sender import send_to_conversation
    from core.plugin_manager import plugin_manager

    config = await plugin_manager.load_config("mod_crm_engine")
    follow_up_hours = getattr(config, "follow_up_hours", 24)
    threshold = datetime.now(timezone.utc) - timedelta(hours=follow_up_hours)
    # Ventana de 24h de WhatsApp: solo leads con actividad en las últimas 24h
    window_limit = datetime.now(timezone.utc) - timedelta(hours=24)

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

    sent = 0
    for lead in leads:
        if lead.conversation_id is None:
            continue
        async with get_session() as session:
            conversation = await session.get(Conversation, lead.conversation_id)
        if conversation is None or conversation.bot_paused or conversation.status != "open":
            continue
        message = FOLLOW_UP_MESSAGE.format(
            name=f" {lead.name}" if lead.name else "", interest=lead.interest or "nuestros productos"
        )
        try:
            await send_to_conversation(lead.conversation_id, message)
            async with get_session() as session:
                fresh = await session.get(Lead, lead.id)
                if fresh is not None:
                    fresh.status = "contactado"
                    fresh.last_activity_at = datetime.now(timezone.utc)
            sent += 1
        except Exception:
            log.exception("follow_up_send_failed", lead_id=str(lead.id))

    log.info("follow_up_completed", candidates=len(leads), sent=sent)
