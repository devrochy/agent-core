"""Cron jobs del CRM.

- follow_up_leads: reengancha leads calificados inactivos dentro de la ventana
  de 24h de WhatsApp (patrón 'cazador de ventas' de agent-sale).
- follow_up_quotes: reengancha cotizaciones en borrador sin convertir dentro de
  la ventana de 3-20h (ag-sale: ventana de mensajería proactiva).
- recordar_seguimientos: avisa a la persona cuando llega la hora de un seguimiento que
  programó y le manda cada mañana «Tu día de ventas» (HU-CRM-22). Sin LLM y sin mensajes
  al cliente.

El decorador @register_cron salta automáticamente si el plugin está inactivo. Un
negocio con el pack habilitado pero sin configurar se salta con un aviso y el cron
sigue con los demás (`_config_del_negocio`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from core.db.session import get_owned, get_session
from core.shared.exceptions import PluginConfigError
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.decorators import register_cron
from pack_commerce.config import FOLLOW_UP_HORAS_MAX, FOLLOW_UP_HORAS_POR_DEFECTO
from pack_commerce.etapas import cambiar_etapa
from pack_commerce.models import Lead, Quote

log = get_logger(__name__)


PACK_ID = "pack_commerce"


async def _tenant_ids() -> list[uuid.UUID]:
    """Tenants cuyo AgentSpec incluye este pack (ADR-0019).

    Sin este filtro, los follow-ups se enviarían a tenants sin el pack.
    """
    from core.agents.spec import enabled_packs, load_active_spec

    async with get_session() as session:
        rows = (
            await session.execute(text("select id from tenants where deleted_at is null"))
        ).scalars().all()

    result: list[uuid.UUID] = []
    for raw in rows:
        tid = uuid.UUID(str(raw))
        try:
            async with tenant_scope(tid):
                spec = await load_active_spec()
            if PACK_ID in enabled_packs(spec):
                result.append(tid)
        except Exception:
            continue
    return result


async def _config_del_negocio(tenant_id: uuid.UUID, kind: str):  # type: ignore[no-untyped-def]
    """Config del pack para el negocio del `tenant_scope` actual, o None si no la tiene.

    Un negocio puede tener el pack habilitado sin haberlo configurado todavía (p. ej.
    a mitad del alta). Sin esto, el `PluginConfigError` de `load_config` cortaba el
    recorrido y los negocios siguientes se quedaban sin seguimiento.
    """
    from core.plugin_manager import plugin_manager

    try:
        return await plugin_manager.load_config(PACK_ID)
    except PluginConfigError:
        log.warning("follow_up_sin_config", kind=kind, tenant_id=str(tenant_id))
        return None


@register_cron("pack_commerce", "0 9 * * *")
async def follow_up_leads() -> None:
    """Reengancha leads calificados inactivos, dentro de la ventana de 24h de WhatsApp."""
    total_candidates = 0
    total_sent = 0
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            config = await _config_del_negocio(tenant_id, "lead")
            if config is None:
                continue
            candidates, sent = await _follow_up_leads_del_negocio(tenant_id, config)
        total_candidates += candidates
        total_sent += sent

    log.info("follow_up_completed", kind="lead", candidates=total_candidates, sent=total_sent)


async def _follow_up_leads_del_negocio(tenant_id: uuid.UUID, config) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """Seguimiento de leads de un negocio (dentro de su `tenant_scope`): (candidatos, enviados)."""
    from core.db.models import Conversation, Customer
    from core.gateway.sender import send_to_conversation

    total_candidates = 0
    total_sent = 0
    # Dentro de la ventana de 24h de WhatsApp el tramo [window_limit, threshold) solo existe si la espera es
    # menor que 24h. Una config vieja con 24 o más (nunca envió nada) se lee como el valor por defecto.
    follow_up_hours = getattr(config, "follow_up_hours", FOLLOW_UP_HORAS_POR_DEFECTO)
    if not 0 < follow_up_hours <= FOLLOW_UP_HORAS_MAX:
        follow_up_hours = FOLLOW_UP_HORAS_POR_DEFECTO
    now = datetime.now(UTC)
    threshold = now - timedelta(hours=follow_up_hours)
    window_limit = now - timedelta(hours=24)

    async with get_session() as session:
        leads = (
            (
                await session.execute(
                    select(Lead).where(
                        Lead.status == "calificado",
                        Lead.last_activity_at < threshold,
                        Lead.last_activity_at > window_limit,
                        Lead.tenant_id == tenant_id,
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
            conversation = await get_owned(session, Conversation, lead.conversation_id, tenant_id)
            # El nombre del cliente es el del perfil (ADR-0149); el del lead, si no hay otro.
            cliente = (
                await get_owned(session, Customer, lead.customer_id, tenant_id) if lead.customer_id else None
            )
        if conversation is None or conversation.bot_paused or conversation.status != "open":
            continue
        nombre = (cliente.name if cliente else None) or lead.name
        template = getattr(config, "follow_up_lead_message", "")
        message = template.format(
            name=f" {nombre}" if nombre else "",
            interest=lead.interest or "nuestros productos",
        )
        try:
            await send_to_conversation(lead.conversation_id, message)
            async with get_session() as session:
                fresh = await get_owned(session, Lead, lead.id, tenant_id)
                if fresh is not None:
                    # Solo si sigue «calificado»: un humano pudo moverlo mientras se enviaba el mensaje.
                    if fresh.status == "calificado":
                        await cambiar_etapa(
                            session,
                            fresh,
                            "contactado",
                            actor="sistema",
                            idempotency_key=f"sistema:seguimiento:{fresh.id}:{datetime.now(UTC):%Y-%m-%d}",
                        )
                    fresh.last_activity_at = datetime.now(UTC)
            total_sent += 1
        except Exception:
            log.exception("follow_up_send_failed", lead_id=str(lead.id))
    return total_candidates, total_sent


@register_cron("pack_commerce", "30 9,15,20 * * *")
async def follow_up_quotes() -> None:
    """Reengancha cotizaciones en borrador de hace 3-20h (ventana proactiva).

    Una cotización se reengancha una sola vez (follow_up_sent_at) y solo si la
    conversación sigue abierta y dentro de la ventana de 24h de WhatsApp.
    """
    total_candidates = 0
    total_sent = 0
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            config = await _config_del_negocio(tenant_id, "quote")
            if config is None:
                continue
            candidates, sent = await _follow_up_quotes_del_negocio(tenant_id, config)
        total_candidates += candidates
        total_sent += sent

    log.info("follow_up_completed", kind="quote", candidates=total_candidates, sent=total_sent)


async def _follow_up_quotes_del_negocio(tenant_id: uuid.UUID, config) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """Seguimiento de cotizaciones de un negocio (dentro de su `tenant_scope`): (candidatas, enviadas)."""
    from core.db.models import Conversation
    from core.gateway.sender import send_to_conversation

    total_candidates = 0
    total_sent = 0
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
                    Quote.tenant_id == tenant_id,
                )
            )
        ).scalars().all()

    total_candidates += len(quotes)
    for quote in quotes:
        async with get_session() as session:
            conversation = await get_owned(session, Conversation, quote.conversation_id, tenant_id)
        if conversation is None or conversation.bot_paused or conversation.status != "open":
            continue
        template = getattr(config, "follow_up_quote_message", "")
        message = template.format(total=f"${float(quote.total):,.0f}")
        try:
            await send_to_conversation(quote.conversation_id, message)
            async with get_session() as session:
                fresh = await get_owned(session, Quote, quote.id, tenant_id)
                if fresh is not None:
                    fresh.follow_up_sent_at = now
            total_sent += 1
        except Exception:
            log.exception("follow_up_quote_send_failed", quote_id=str(quote.id))
    return total_candidates, total_sent


@register_cron("pack_commerce", "*/5 * * * *")
async def recordar_seguimientos() -> None:
    """Recordatorios de seguimiento y resumen diario de ventas (HU-CRM-22).

    Cada 5 minutos, por negocio y dentro de su `tenant_scope`. Es idempotente por la clave de
    deduplicación de cada aviso (ver `recordatorios`): correrlo de más o tras una caída no repite
    nada. Un negocio que falla no corta a los demás."""
    from pack_commerce import recordatorios

    total = recordatorios.Conteo()
    resumenes = 0
    for tenant_id in await _tenant_ids():
        try:
            async with tenant_scope(tenant_id):
                config = await _config_del_negocio(tenant_id, "seguimiento")
                if config is None:
                    continue
                conteo, resumen = await _recordar_del_negocio(tenant_id, config)
            total.sumar(conteo)
            resumenes += int(resumen)
        except Exception:
            log.exception("recordar_seguimientos_fallo", tenant_id=str(tenant_id))
    log.info(
        "recordar_seguimientos_completed",
        enviados=total.enviados,
        ya_avisados=total.ya_avisados,
        saltados_horario=total.saltados_horario,
        saltados_resueltos=total.saltados_resueltos,
        resumenes=resumenes,
    )


async def _recordar_del_negocio(tenant_id: uuid.UUID, config, ahora: datetime | None = None):  # type: ignore[no-untyped-def]
    """Recordatorios y resumen de un negocio (dentro de su `tenant_scope`): (conteo, ¿salió el resumen?)."""
    from core.db.models import AppSettings
    from core.orchestrator.escalation import within_business_hours
    from pack_commerce import recordatorios, seguimiento

    ahora = ahora or datetime.now(UTC)
    conteo = recordatorios.Conteo()
    resumen = False
    async with get_session() as session:
        zona = await seguimiento.zona_del_negocio(session, tenant_id)
        if getattr(config, "seguimiento_recordatorios", True):
            escalacion = (
                await session.execute(
                    select(AppSettings.escalation_config).where(
                        AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            horario = (escalacion or {}).get("business_hours")
            conteo = await recordatorios.recordar_vencidos(
                session, tenant_id, ahora=ahora, zona=zona, abierto=within_business_hours(horario, ahora)
            )
        if getattr(config, "seguimiento_resumen_diario", True):
            resumen = await recordatorios.resumen_diario(
                session, tenant_id, ahora=ahora, zona=zona, hora=getattr(config, "seguimiento_resumen_hora", 8)
            )
    if conteo.saltados_horario or conteo.saltados_resueltos:
        log.info(
            "recordar_seguimientos_saltados",
            tenant_id=str(tenant_id),
            saltados_horario=conteo.saltados_horario,
            saltados_resueltos=conteo.saltados_resueltos,
        )
    return conteo, resumen
