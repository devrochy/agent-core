"""Contribución del pack a la línea de tiempo del cliente (ADR-0056, HU-CRM-04).

Sin columnas ni filtros en Clientes: solo las citas del cliente en su línea de tiempo.
Importar este módulo (lo hace `module.py`) registra el provider.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.admin.lead_registry import Cursor, TimelineEvent, antes_del_cursor, lead_registry
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id
from pack_calendar.models import Appointment

PLUGIN_ID = "pack_calendar"
_ESTADO = {"scheduled": "agendada", "cancelled": "cancelada", "completed": "atendida", "no_show": "no asistió"}


async def _timeline(customer_id: uuid.UUID, cursor: Cursor | None, limit: int) -> list[TimelineEvent]:
    """Citas del cliente por fecha de creación (cuándo se agendó), con la fecha de la cita."""
    tid = get_current_tenant_id()
    async with get_session() as session:
        citas = (
            await session.execute(
                select(Appointment)
                .where(
                    Appointment.tenant_id == tid,
                    Appointment.customer_id == customer_id,
                    Appointment.deleted_at.is_(None),
                    antes_del_cursor(Appointment.created_at, Appointment.id, "pack_calendar.cita", cursor),
                )
                .order_by(Appointment.created_at.desc(), Appointment.id.desc())
                .limit(limit)
            )
        ).scalars().all()
    return [
        TimelineEvent(
            at=c.created_at,
            source="pack_calendar.cita",
            id=str(c.id),
            kind="cita",
            title=f"Cita {_ESTADO.get(c.status, c.status)}: {c.title or 'sin título'}",
            detail=c.start_at.isoformat(),
            link_kind="appointment",
            link_id=str(c.id),
        )
        for c in citas
    ]


lead_registry.register(PLUGIN_ID, columns=[], resolver=None, timeline=_timeline)
