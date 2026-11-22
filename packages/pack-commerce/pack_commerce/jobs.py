"""Cron jobs del CRM.

- follow_up_quotes: reengancha cotizaciones en borrador sin convertir dentro de
  la ventana de 3-20h (ag-sale: ventana de mensajería proactiva).
Los crons de leads (reenganche y recordatorios de seguimiento) son del core desde ADR-0193 (`core.leads.jobs`).

El reenganche de cotizaciones es de campañas completas (Premium y Empresarial, HU-BILL-26, decisión del dueño del
2026-11-22): en un plan sin ellas no se escribe a nadie, la cotización no se marca y la tarjeta del cron queda
`skipped` con el motivo. Una cotización que sigue dentro de su ventana cuando el negocio sube de plan recibe su
PRIMER reenganche (nunca salió nada; no es un reenvío).

El decorador @register_cron salta automáticamente si el plugin está inactivo. Un
negocio con el pack habilitado pero sin configurar se salta con un aviso y el cron
sigue con los demás (`_config_del_negocio`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from core.billing.campanas_del_plan import COMPLETAS, plan_que_frena
from core.billing.campanas_del_plan import MOTIVO as MOTIVO_PLAN
from core.db.session import get_owned, get_session
from core.gateway import pausa_automatica
from core.shared.exceptions import PluginConfigError
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.cron_negocio import debe_correr, registrar
from core.tasks.decorators import register_cron
from pack_commerce.models import Quote
from pack_commerce.vivos import cotizacion_viva

log = get_logger(__name__)


PACK_ID = "pack_commerce"

CRON_QUOTES = f"{PACK_ID}.follow_up_quotes"


async def _tenant_ids() -> list[uuid.UUID]:
    """Tenants con este pack entre los packs del negocio (`packs_del_negocio`, ADR-0206 §5).

    Sin este filtro, los follow-ups se enviarían a tenants sin el pack.
    """
    from core.agents.instalaciones_agente import packs_del_negocio

    async with get_session() as session:
        rows = (
            await session.execute(text("select id from tenants where deleted_at is null"))
        ).scalars().all()

    result: list[uuid.UUID] = []
    for raw in rows:
        tid = uuid.UUID(str(raw))
        try:
            async with tenant_scope(tid):
                if PACK_ID in await packs_del_negocio(tid):
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
            # Con el asistente en pausa no sale ningún seguimiento (HU-ARCH-37) y no se gasta la marca.
            if await pausa_automatica.negocio_pausado(tenant_id):
                log.info("follow_up_negocio_pausado", kind="quote", tenant_id=str(tenant_id))
                continue
            if not await debe_correr(tenant_id, CRON_QUOTES):
                continue
            config = await _config_del_negocio(tenant_id, "quote")
            if config is None:
                continue
            # Campañas completas (HU-BILL-26). Sin suscripción o con un plan desconocido, None: sale.
            if plan := await plan_que_frena(tenant_id, COMPLETAS):
                log.info("follow_up_frenado_por_plan", kind="quote", tenant_id=str(tenant_id), motivo=MOTIVO_PLAN, plan=plan)
                await registrar(
                    tenant_id, CRON_QUOTES, acciones=0, status="skipped",
                    resumen="Sin enviar: tu plan no incluye el seguimiento automático de cotizaciones",
                )
                continue
            candidates, sent = await _follow_up_quotes_del_negocio(tenant_id, config)
            await registrar(
                tenant_id, CRON_QUOTES, acciones=sent,
                resumen=f"{sent} cotizaciones reenganchadas de {candidates} candidatas",
            )
        total_candidates += candidates
        total_sent += sent

    log.info("follow_up_completed", kind="quote", candidates=total_candidates, sent=total_sent)


async def _follow_up_quotes_del_negocio(tenant_id: uuid.UUID, config) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """Seguimiento de cotizaciones de un negocio (dentro de su `tenant_scope`): (candidatas, enviadas)."""
    from core.db.models import Conversation, Customer
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
                    Quote.deleted_at.is_(None),
                )
            )
        ).scalars().all()

    total_candidates += len(quotes)
    for quote in quotes:
        async with get_session() as session:
            conversation = await get_owned(session, Conversation, quote.conversation_id, tenant_id)
            cliente = (
                await get_owned(session, Customer, conversation.customer_id, tenant_id) if conversation else None
            )
        if conversation is None or conversation.bot_paused or conversation.status != "open":
            continue
        if cliente is not None and cliente.bot_paused:
            # Sin llegar al sender: si no, cada corrida dejaría otro `failed` mientras dure la ventana.
            continue
        template = getattr(config, "follow_up_quote_message", "")
        message = template.format(total=f"${float(quote.total):,.0f}")
        try:
            # Solo se marca `follow_up_sent_at` si salió (HU-ARCH-37); el guardián del envío frena al cliente en pausa.
            if not await send_to_conversation(quote.conversation_id, message):
                continue
            async with get_session() as session:
                fresh = await cotizacion_viva(session, quote.id, tenant_id)
                if fresh is not None:
                    fresh.follow_up_sent_at = now
            total_sent += 1
        except Exception:
            log.exception("follow_up_quote_send_failed", quote_id=str(quote.id))
    return total_candidates, total_sent
