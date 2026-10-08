"""Demo (cita) de punta a punta (HU-SH-02): sin doble reserva, confirmación con zona y
enlace, aviso al equipo y datos de la cita para los avisos al cliente.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

RESTRICCION_SIN_SOLAPE = "ex_pack_calendar_appointments_sin_solape"


class HorarioOcupado(Exception):
    """La base rechazó la cita: otra quedó en ese horario primero (dos reservas a la vez)."""


def es_solape(exc: Exception) -> bool:
    return isinstance(exc, IntegrityError) and RESTRICCION_SIN_SOLAPE in str(getattr(exc, "orig", exc))


def enlace_de(appt_type: Any, config: Any) -> str:
    """Enlace de la reunión: el del tipo de cita o, si no tiene, el general del negocio."""
    return (getattr(appt_type, "enlace_reunion", None) or getattr(config, "enlace_reunion", "") or "").strip()


def datos_de_cita(titulo: str, inicio: datetime, zona: str, enlace: str) -> dict[str, str]:
    """Fecha, hora y zona en la hora del negocio (para mensajes y plantillas)."""
    from pack_calendar.skills import _long_date

    local = inicio.astimezone(ZoneInfo(zona))
    return {"titulo": titulo, "fecha": _long_date(local.date()), "hora": local.strftime("%H:%M"), "zona": zona, "enlace": enlace}


def texto_confirmacion(plantilla: str, datos: dict[str, str]) -> str:
    """Confirmación al cliente con fecha, hora, zona y enlace (si hay)."""
    from pack_calendar.skills import _KeepUnknown

    enlace = f"\n🔗 Enlace de la reunión: {datos['enlace']}" if datos.get("enlace") else ""
    return plantilla.format_map(_KeepUnknown({**datos, "enlace": enlace}))


async def emitir_cambio(session, *, appointment_id: uuid.UUID, accion: str, titulo: str, inicio: datetime | None, evento_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    """Aviso al equipo por el outbox (creada | reagendada | cancelada), en la MISMA
    transacción que el historial de la cita."""
    from core.events.outbox import emit_domain
    from core.shared.events import ON_APPOINTMENT_CHANGED
    from core.shared.tenant import get_current_tenant_id

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return
    await emit_domain(
        session,
        tenant_id=tenant_id,
        topic=ON_APPOINTMENT_CHANGED,
        dedupe_key=f"appt_changed:{evento_id}",
        payload={
            "appointment_id": str(appointment_id),
            "accion": accion,
            "titulo": titulo,
            "inicio": inicio.isoformat() if inicio else None,
        },
    )


async def avisar_al_equipo(appointment_id: str, accion: str, titulo: str, inicio: str | None) -> None:
    """Push al colaborador asignado a la cita o, si no tiene, a los dueños del negocio."""
    from core.db.session import get_session
    from core.shared.logger import get_logger
    from core.shared.tenant import get_current_tenant_id
    from pack_calendar.models import Appointment

    log = get_logger(__name__)
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return
    async with get_session() as session:
        cita = await session.get(Appointment, uuid.UUID(appointment_id))
    if cita is None or cita.tenant_id != tenant_id:
        return
    cuando = ""
    if inicio:
        local = datetime.fromisoformat(inicio).astimezone(ZoneInfo(cita.timezone or "America/Bogota"))
        cuando = f" para el {local:%d/%m} a las {local:%H:%M}"
    verbo = {"programada": "nueva", "reprogramada": "reagendada", "cancelada": "cancelada"}.get(accion, accion)
    titulo_push = f"Cita {verbo}: {titulo or cita.title}"
    cuerpo = f"{cita.title}{cuando}."
    data = {"kind": "cita", "appointment_id": appointment_id, "accion": accion}
    try:
        from core.tasks.push_tasks import notify_owners_push, send_push_batch

        if cita.asignado_admin_id:
            send_push_batch.delay(str(tenant_id), [str(cita.asignado_admin_id)], titulo_push, cuerpo, data)
        else:
            notify_owners_push.delay(str(tenant_id), titulo_push, cuerpo, data)
    except Exception:
        log.warning("cita_aviso_equipo_fallido", appointment_id=appointment_id, exc_info=True)
