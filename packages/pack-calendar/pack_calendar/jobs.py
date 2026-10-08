"""Cron jobs de pack-calendar (HU-SH-02).

- `recordatorios_de_citas` (cada 15 min): recuerda la cita al cliente `recordatorios_horas`
  antes (por defecto 24 h y 1 h), una sola vez cada uno (`dedupe_key`), por el outbox: el
  core la envía como plantilla o como texto según la ventana de 24 h (ADR-0108).

El decorador `@register_cron` salta si el pack está inactivo; el filtro por negocio
habilitado es explícito (`_tenant_ids`, ADR-0019).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from core.db.session import get_owned, get_session
from core.events.outbox import emit_domain
from core.shared.events import ON_APPOINTMENT_REMINDER
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.decorators import register_cron
from pack_calendar.models import Appointment, AppointmentType

log = get_logger(__name__)

PACK_ID = "pack_calendar"


async def _tenant_ids() -> list[uuid.UUID]:
    """Negocios cuyo AgentSpec incluye este pack (ADR-0019)."""
    from core.agents.spec import enabled_packs, load_active_spec

    async with get_session() as session:
        rows = (await session.execute(text("select id from tenants where deleted_at is null"))).scalars().all()
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
            config = await _config()
            horas = [int(h) for h in (config.recordatorios_horas or []) if int(h) > 0]
            if not horas:
                continue
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
                        },
                    )
                    enviados += int(insertado)
    if enviados:
        log.info("citas_recordatorios", enviados=enviados)
    return enviados
