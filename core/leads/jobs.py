"""Crons de Captar leads (ADR-0193 §3, HU-AOS-70): vivían en `pack_commerce.jobs`.

- `core.leads_follow_up`: reengancha leads calificados inactivos dentro de la ventana de 24 h de WhatsApp.
- `core.leads_recordar_seguimientos`: avisa a la persona cuando llega la hora de un seguimiento que programó y le
  manda cada mañana «Tu día de ventas» (HU-CRM-22). Sin LLM y sin mensajes al cliente.

Solo corren en los negocios con la config de Captar leads (`lead_settings`): sin fila no hacen nada, la misma conducta
que antes tenía un negocio sin commerce. Respetan la pausa del negocio (HU-ARCH-37) y la política del negocio de cada
cron (`cron_negocio`). Un negocio que falla no corta a los demás.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from core.db.models import Lead, LeadSettings
from core.db.session import get_owned, get_session
from core.gateway import pausa_automatica
from core.leads import config as config_leads
from core.leads.config import FOLLOW_UP_HORAS_MAX, FOLLOW_UP_HORAS_POR_DEFECTO
from core.leads.etapas import cambiar_etapa
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.cron_negocio import debe_correr, registrar, registrar_fallo

log = get_logger(__name__)

CRON_LEADS = "core.leads_follow_up"
CRON_SEGUIMIENTOS = "core.leads_recordar_seguimientos"


async def _negocios_con_captar_leads() -> list[uuid.UUID]:
    """Negocios con config de Captar leads viva. `lead_settings` tiene RLS forzada: se mira negocio por negocio,
    dentro de su `tenant_scope`."""
    from core.db.models import Tenant

    async with get_session() as session:
        negocios = (await session.execute(select(Tenant.id).where(Tenant.deleted_at.is_(None)))).scalars().all()
    con_config: list[uuid.UUID] = []
    for tid in negocios:
        async with tenant_scope(tid):
            async with get_session() as session:
                fila = (
                    await session.execute(
                        select(LeadSettings.tenant_id).where(
                            LeadSettings.tenant_id == tid, LeadSettings.deleted_at.is_(None)
                        )
                    )
                ).scalar_one_or_none()
        if fila is not None:
            con_config.append(tid)
    return con_config


async def follow_up_leads() -> None:
    """Reengancha leads calificados inactivos, dentro de la ventana de 24 h de WhatsApp."""
    total_candidates = 0
    total_sent = 0
    for tenant_id in await _negocios_con_captar_leads():
        try:
            async with tenant_scope(tenant_id):
                # Con el asistente en pausa no sale ningún seguimiento (HU-ARCH-37) y no se gasta la marca.
                if await pausa_automatica.negocio_pausado(tenant_id):
                    log.info("follow_up_negocio_pausado", kind="lead", tenant_id=str(tenant_id))
                    continue
                if not await debe_correr(tenant_id, CRON_LEADS):
                    continue
                config = await config_leads.cargar_del_negocio(tenant_id)
                if config is None:
                    continue
                candidates, sent = await _follow_up_leads_del_negocio(tenant_id, config)
                await registrar(
                    tenant_id, CRON_LEADS, acciones=sent,
                    resumen=f"{sent} leads reenganchados de {candidates} candidatos",
                )
            total_candidates += candidates
            total_sent += sent
        except Exception as exc:
            log.exception("follow_up_leads_fallo", tenant_id=str(tenant_id))
            await registrar_fallo(tenant_id, CRON_LEADS, error_class=type(exc).__name__)
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
                        Lead.deleted_at.is_(None),
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
        if cliente is not None and cliente.bot_paused:
            continue
        nombre = (cliente.name if cliente else None) or lead.name
        template = getattr(config, "follow_up_lead_message", "")
        message = template.format(
            name=f" {nombre}" if nombre else "",
            interest=lead.interest or "nuestros productos",
        )
        try:
            # Solo se marca si salió (HU-ARCH-37): si no (pausa, ventana cerrada, conexión pausada), el lead sigue
            # siendo candidato mientras esté dentro de su ventana.
            if not await send_to_conversation(lead.conversation_id, message):
                continue
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


async def recordar_seguimientos() -> None:
    """Recordatorios de seguimiento y resumen diario de ventas (HU-CRM-22), cada 5 minutos y por negocio. Es
    idempotente por la clave de deduplicación de cada aviso: correrlo de más o tras una caída no repite nada."""
    from core.leads import recordatorios

    total = recordatorios.Conteo()
    resumenes = 0
    for tenant_id in await _negocios_con_captar_leads():
        try:
            async with tenant_scope(tenant_id):
                if not await debe_correr(tenant_id, CRON_SEGUIMIENTOS):
                    continue
                config = await config_leads.cargar_del_negocio(tenant_id)
                if config is None:
                    continue
                conteo, resumen = await _recordar_del_negocio(tenant_id, config)
                await registrar(
                    tenant_id, CRON_SEGUIMIENTOS, acciones=conteo.enviados,
                    resumen=f"{conteo.enviados} recordatorios, {int(resumen)} resúmenes",
                )
            total.sumar(conteo)
            resumenes += int(resumen)
        except Exception as exc:
            log.exception("recordar_seguimientos_fallo", tenant_id=str(tenant_id))
            await registrar_fallo(tenant_id, CRON_SEGUIMIENTOS, error_class=type(exc).__name__)
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
    from core.leads import recordatorios, seguimiento
    from core.orchestrator.escalation import within_business_hours

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
