"""Adapters de sync del pack de Calendario (ADR-0097).

Entidades: `citas` (`pack_calendar:citas`), `tipos_cita` (`pack_calendar:tipos`)
y `bloqueos` (`pack_calendar:bloqueos`), todas con `updated_at` y soft delete.
"""

from __future__ import annotations

from typing import Any

from core.sync import SyncAdapter, register_sync
from pack_calendar.models import Appointment, AppointmentType, CalendarBlock


def _serializar_cita(row: Appointment) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "titulo": row.title,
        "tipoCitaId": str(row.appointment_type_id) if row.appointment_type_id else None,
        "clienteId": str(row.customer_id) if row.customer_id else None,
        "asistenteEmail": row.attendee_email,
        "inicio": row.start_at.isoformat(),
        "fin": row.end_at.isoformat(),
        "estado": row.status,
        "notas": row.notes,
        "zonaHoraria": row.timezone,
        "updatedAt": row.updated_at.isoformat(),
    }


def _serializar_tipo_cita(row: AppointmentType) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "nombre": row.name,
        "duracionMinutos": row.duration_minutes,
        "activo": row.active,
        "updatedAt": row.updated_at.isoformat(),
    }


def _serializar_bloqueo(row: CalendarBlock) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "inicio": row.start_at.isoformat(),
        "fin": row.end_at.isoformat(),
        "motivo": row.reason,
        "updatedAt": row.updated_at.isoformat(),
    }


register_sync(
    SyncAdapter(
        entidad="citas",
        seccion="pack_calendar:citas",
        label="Citas",
        model=Appointment,
        marcador_sql=Appointment.updated_at,
        marcador_row=lambda row: row.updated_at,
        serialize=_serializar_cita,
        tiene_deleted_at=True,
    )
)

register_sync(
    SyncAdapter(
        entidad="tipos_cita",
        seccion="pack_calendar:tipos",
        label="Tipos de cita",
        model=AppointmentType,
        marcador_sql=AppointmentType.updated_at,
        marcador_row=lambda row: row.updated_at,
        serialize=_serializar_tipo_cita,
        tiene_deleted_at=True,
    )
)

register_sync(
    SyncAdapter(
        entidad="bloqueos",
        seccion="pack_calendar:bloqueos",
        label="Bloqueos",
        model=CalendarBlock,
        marcador_sql=CalendarBlock.updated_at,
        marcador_row=lambda row: row.updated_at,
        serialize=_serializar_bloqueo,
        tiene_deleted_at=True,
    )
)
