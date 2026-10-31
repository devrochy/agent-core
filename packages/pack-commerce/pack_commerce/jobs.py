"""Cron jobs del CRM.

- follow_up_leads: reengancha leads calificados inactivos dentro de la ventana
  de 24h de WhatsApp (patrón 'cazador de ventas' de agent-sale).
- follow_up_quotes: reengancha cotizaciones en borrador sin convertir dentro de
  la ventana de 3-20h (ag-sale: ventana de mensajería proactiva).

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


@register_cron(PACK_ID, "*/10 * * * *")
async def reintentar_activaciones() -> None:
    """Reintenta la activación de planes pagados que quedaron pendientes (HU-SUB-09)."""
    await _reintentar_activaciones()


async def _reintentar_activaciones() -> int:
    from pack_commerce.models import Order
    from pack_commerce.planes import activar_planes_del_pedido

    total = 0
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                ids = (
                    await session.execute(
                        select(Order.id).where(
                            Order.tenant_id == tenant_id,
                            Order.payment_status == "pagado",
                            Order.plan_activation_status == "pendiente",
                        )
                    )
                ).scalars().all()
            # Un pedido (o un negocio) que falla no frena a los demás: cada pedido va
            # en su propia transacción.
            for order_id in ids:
                try:
                    async with get_session() as session:
                        order = await session.get(Order, order_id)
                        if order is None:
                            continue
                        await activar_planes_del_pedido(session, order)
                        if order.plan_activation_status == "activada":
                            total += 1
                except Exception:
                    log.exception("reintento_activacion_fallido", tenant_id=str(tenant_id), order_id=str(order_id))
    if total:
        log.info("planes_activados_al_reintentar", total=total)
    return total


@register_cron(PACK_ID, "5 * * * *")
async def sincronizar_precios_de_plan() -> None:
    """Precio de los planes de la plataforma desde core/billing (HU-SH-01)."""
    await _sincronizar_precios_de_plan()


async def _sincronizar_precios_de_plan() -> int:
    from pack_commerce.precios_de_plan import sincronizar

    total = 0
    for tenant_id in await _tenant_ids():
        try:
            async with tenant_scope(tenant_id):
                async with get_session() as session:
                    total += await sincronizar(session, tenant_id)
        except Exception:
            log.exception("precios_de_plan_fallido", tenant_id=str(tenant_id))
    if total:
        log.info("precios_de_plan_sincronizados", total=total)
    return total
