"""Providers de búsqueda del pack de Calendario (ADR-0096).

Sección `pack_calendar:citas`: busca por título, correo del asistente o notas
de la cita. RLS aplica sobre `pack_calendar_appointments`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import or_, select

from core.db.session import get_session
from core.search import ResultadoBusqueda, escapar_ilike, register_search
from pack_calendar.models import Appointment


async def _buscar_citas(tenant_id: uuid.UUID, q: str, limit: int) -> list[ResultadoBusqueda]:
    patron = f"%{escapar_ilike(q)}%"
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Appointment)
                .where(
                    Appointment.tenant_id == tenant_id,
                    Appointment.deleted_at.is_(None),
                    or_(
                        Appointment.title.ilike(patron, escape="\\"),
                        Appointment.attendee_email.ilike(patron, escape="\\"),
                        Appointment.notes.ilike(patron, escape="\\"),
                    ),
                )
                .order_by(Appointment.start_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    return [
        {
            "id": str(a.id),
            "tipo": "cita",
            "titulo": a.title or "(sin título)",
            "subtitulo": f"Cita · {a.start_at.isoformat()}",
            "extra": {"estado": a.status, "asistente": a.attendee_email},
        }
        for a in rows
    ]


register_search("pack_calendar:citas", "Citas", _buscar_citas)
