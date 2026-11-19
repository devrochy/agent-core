"""Datos de ejemplo de la agenda (`seed_data`, ADR-0083): tipos de cita, citas y bloqueos.

Lo usan los datos de prueba de cada tester (HU-QA-07) con el fragmento `presets/qa.json`.
Idempotente: un tipo se reconoce por su nombre, una cita por su título y su inicio, y un
bloqueo por su motivo y su inicio.

Formato de `data` (las fechas son relativas a hoy, en la zona del negocio):

    {"timezone": "America/Bogota",
     "tipos": [{"name": "Asesoría", "duration_minutes": 30, "description": "..."}],
     "citas": [{"tipo": "Asesoría", "en_dias": 1, "hora": "10:00", "title": "Asesoría con Juana"}],
     "bloqueos": [{"en_dias": 2, "desde": "12:00", "hasta": "14:00", "reason": "Almuerzo"}]}
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from core.db.session import get_session
from core.shared.tenant import tenant_scope
from pack_calendar.models import Appointment, AppointmentType, CalendarBlock


def _instante(en_dias: Any, hora: Any, zona: ZoneInfo) -> datetime:
    dia = date.today() + timedelta(days=int(en_dias or 0))
    return datetime.combine(dia, time.fromisoformat(str(hora or "09:00")), tzinfo=zona)


async def sembrar_agenda(
    tenant_id: uuid.UUID, data: dict[str, Any], creados: list[tuple[str, uuid.UUID]] | None = None
) -> list[str]:
    """`creados` (datos de prueba, HU-QA-10): recibe `(kind, id)` de cada fila que se crea."""
    nombre_zona = str(data.get("timezone") or "America/Bogota")
    zona = ZoneInfo(nombre_zona)
    pasos: list[str] = []
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            tipos: dict[str, AppointmentType] = {}
            for item in data.get("tipos") or []:
                nombre = str(item.get("name") or "").strip()
                if not nombre:
                    continue
                tipo = (
                    await session.execute(
                        select(AppointmentType).where(
                            AppointmentType.tenant_id == tenant_id,
                            AppointmentType.name == nombre,
                            AppointmentType.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if tipo is None:
                    tipo = AppointmentType(
                        tenant_id=tenant_id,
                        name=nombre,
                        duration_minutes=int(item.get("duration_minutes") or 60),
                        description=str(item.get("description") or ""),
                    )
                    session.add(tipo)
                    await session.flush()
                    if creados is not None:
                        creados.append(("tipo", tipo.id))
                    pasos.append(f"tipo de cita {nombre} creado")
                tipos[nombre] = tipo

            for item in data.get("citas") or []:
                tipo = tipos.get(str(item.get("tipo") or ""))
                titulo = str(item.get("title") or (tipo.name if tipo else "Cita")).strip()
                inicio = _instante(item.get("en_dias"), item.get("hora"), zona)
                existe = (
                    await session.execute(
                        select(Appointment.id).where(
                            Appointment.tenant_id == tenant_id,
                            Appointment.title == titulo,
                            Appointment.start_at == inicio,
                            Appointment.deleted_at.is_(None),
                        )
                    )
                ).first()
                if existe is not None:
                    continue
                minutos = int(item.get("duration_minutes") or (tipo.duration_minutes if tipo else 60))
                cita = Appointment(
                    tenant_id=tenant_id,
                    appointment_type_id=tipo.id if tipo else None,
                    title=titulo,
                    start_at=inicio,
                    end_at=inicio + timedelta(minutes=minutos),
                    notes=str(item.get("notes") or ""),
                    created_by="panel",
                    timezone=nombre_zona,
                )
                session.add(cita)
                if creados is not None:
                    await session.flush()
                    creados.append(("cita", cita.id))
                pasos.append(f"cita {titulo} creada")

            for item in data.get("bloqueos") or []:
                inicio = _instante(item.get("en_dias"), item.get("desde"), zona)
                fin = _instante(item.get("en_dias"), item.get("hasta"), zona)
                motivo = str(item.get("reason") or "")
                if fin <= inicio:
                    continue
                existe = (
                    await session.execute(
                        select(CalendarBlock.id).where(
                            CalendarBlock.tenant_id == tenant_id,
                            CalendarBlock.reason == motivo,
                            CalendarBlock.start_at == inicio,
                            CalendarBlock.deleted_at.is_(None),
                        )
                    )
                ).first()
                if existe is None:
                    bloqueo = CalendarBlock(tenant_id=tenant_id, start_at=inicio, end_at=fin, reason=motivo)
                    session.add(bloqueo)
                    if creados is not None:
                        await session.flush()
                        creados.append(("bloqueo", bloqueo.id))
                    pasos.append(f"bloqueo {motivo or 'sin motivo'} creado")
    return pasos
