"""Cron jobs de pack-calendar (HU-SH-02).

- `recordatorios_de_citas` (cada 15 min): recuerda la cita al cliente `recordatorios_horas`
  antes (por defecto 24 h y 1 h), una sola vez cada uno (`dedupe_key`), por el outbox: el
  core la envía como plantilla o como texto según la ventana de 24 h (ADR-0108).

  Solo con recordatorios en el plan (`features.campanas`, desde Basic, HU-BILL-26). En un plan
  sin ellos (Free) el recordatorio no sale pero su evento se emite igual, marcado `frenado`: el
  core lo deja como mensaje `failed` con el motivo bajo el id estable del aviso y la `dedupe_key`
  queda consumida, así que al subir de plan no se reenvía (mismo criterio que la pausa, D-76).

El decorador `@register_cron` salta si el pack está inactivo; el filtro por negocio
habilitado es explícito (`_tenant_ids`, ADR-0019).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from core.billing.campanas_del_plan import MOTIVO as MOTIVO_PLAN
from core.billing.campanas_del_plan import RECORDATORIOS, plan_que_frena
from core.db.session import get_owned, get_session
from core.events.outbox import emit_domain
from core.shared.events import ON_APPOINTMENT_REMINDER
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.cron_negocio import debe_correr, registrar
from core.tasks.decorators import register_cron
from pack_calendar.models import Appointment, AppointmentType

log = get_logger(__name__)

PACK_ID = "pack_calendar"

CRON_RECORDATORIOS = f"{PACK_ID}.recordatorios_de_citas"


async def _tenant_ids() -> list[uuid.UUID]:
    """Negocios con este pack entre los packs del negocio (`packs_del_negocio`, ADR-0206 §5)."""
    from core.agents.instalaciones_agente import packs_del_negocio

    async with get_session() as session:
        rows = (await session.execute(text("select id from tenants where deleted_at is null"))).scalars().all()
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


async def _config():  # type: ignore[no-untyped-def]
    from core.plugin_manager import plugin_manager
    from pack_calendar.config import CalendarConfig

    try:
        config = await plugin_manager.load_config(PACK_ID)
    except Exception:
        config = None
    return config if isinstance(config, CalendarConfig) else CalendarConfig()


def recordatorio_que_toca(inicio: datetime, creada: datetime, ahora: datetime, horas: list[int]) -> int | None:
    """Recordatorio (en horas) que corresponde enviar ahora, o None.

    Se envía entre `inicio − h` y la mitad de ese plazo (mín. 15 min), y solo si la cita
    ya existía en `inicio − h` (una cita agendada para dentro de 2 h no recibe el de 24 h).
    """
    for h in sorted({int(x) for x in horas if int(x) > 0}, reverse=True):
        momento = inicio - timedelta(hours=h)
        ventana = max(timedelta(hours=h) / 2, timedelta(minutes=15))
        if momento <= ahora < min(momento + ventana, inicio) and creada <= momento:
            return h
    return None


@register_cron(PACK_ID, "*/15 * * * *")
async def recordatorios_de_citas() -> None:
    await _recordatorios_de_citas(datetime.now(UTC))


async def _recordatorios_de_citas(now: datetime) -> int:
    from pack_calendar.demo import datos_de_cita, enlace_de

    enviados = 0
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            if not await debe_correr(tenant_id, CRON_RECORDATORIOS, ahora=now):
                continue
            enviados_negocio = 0
            frenados_negocio = 0
            config = await _config()
            horas = [int(h) for h in (config.recordatorios_horas or []) if int(h) > 0]
            if not horas:
                continue
            # Recordatorios desde Basic (HU-BILL-26). Sin suscripción o con un plan desconocido, None: sale.
            plan_frena = await plan_que_frena(tenant_id, RECORDATORIOS)
            async with get_session() as session:
                citas = (
                    await session.execute(
                        select(Appointment).where(
                            Appointment.tenant_id == tenant_id,
                            Appointment.status == "scheduled",
                            Appointment.deleted_at.is_(None),
                            Appointment.customer_id.isnot(None),
                            Appointment.start_at > now,
                            Appointment.start_at <= now + timedelta(hours=max(horas)),
                        )
                    )
                ).scalars().all()
                for cita in citas:
                    h = recordatorio_que_toca(cita.start_at, cita.created_at, now, horas)
                    if h is None:
                        continue
                    tipo = (
                        await get_owned(session, AppointmentType, cita.appointment_type_id, tenant_id)
                        if cita.appointment_type_id
                        else None
                    )
                    datos = datos_de_cita(cita.title, cita.start_at, cita.timezone or config.timezone, enlace_de(tipo, config))
                    insertado = await emit_domain(
                        session,
                        tenant_id=tenant_id,
                        topic=ON_APPOINTMENT_REMINDER,
                        dedupe_key=f"appt_reminder:{cita.id}:{h}",
                        payload={
                            "appointment_id": str(cita.id),
                            "customer_id": str(cita.customer_id),
                            "horas": h,
                            **datos,
                            "texto": config.recordatorio_texto,
                            # El core no lo envía y lo deja `failed` con este motivo (HU-BILL-26).
                            **({"frenado": MOTIVO_PLAN} if plan_frena else {}),
                        },
                    )
                    if plan_frena:
                        frenados_negocio += int(insertado)
                    else:
                        enviados += int(insertado)
                        enviados_negocio += int(insertado)
            if frenados_negocio:
                log.info(
                    "citas_recordatorios_frenados", tenant_id=str(tenant_id), motivo=MOTIVO_PLAN,
                    plan=plan_frena, frenados=frenados_negocio,
                )
                await registrar(
                    tenant_id, CRON_RECORDATORIOS, acciones=0, status="skipped",
                    resumen=f"{frenados_negocio} recordatorios no enviados: tu plan no incluye recordatorios",
                )
                continue
            await registrar(
                tenant_id, CRON_RECORDATORIOS, acciones=enviados_negocio,
                resumen=f"{enviados_negocio} recordatorios enviados",
            )
    if enviados:
        log.info("citas_recordatorios", enviados=enviados)
    return enviados
