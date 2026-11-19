"""Ficha y detalle de Agenda para el cliente (HU-CRM-14, ADR-0179 decisión 3).

La ficha dice si tiene una cita próxima y cuántas lleva; sin correo ni notas de la cita.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id
from pack_calendar.models import Appointment


def _local(cita: Appointment) -> str:
    try:
        zona = ZoneInfo(cita.timezone or "UTC")
    except Exception:
        zona = UTC
    return cita.start_at.astimezone(zona).strftime("%Y-%m-%d %H:%M")


async def ficha(ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    tid = get_current_tenant_id()
    ahora = datetime.now(UTC)
    base = (Appointment.tenant_id == tid, Appointment.deleted_at.is_(None), Appointment.customer_id.in_(ids))
    async with get_session() as session:
        totales = dict(
            (await session.execute(select(Appointment.customer_id, func.count(Appointment.id)).where(*base).group_by(Appointment.customer_id))).all()
        )
        proximas: dict[uuid.UUID, Appointment] = {}
        for cita in (
            await session.execute(
                select(Appointment)
                .where(*base, Appointment.status == "scheduled", Appointment.start_at >= ahora)
                .order_by(Appointment.start_at.asc())
            )
        ).scalars():
            proximas.setdefault(cita.customer_id, cita)
    salida: dict[uuid.UUID, str] = {}
    for cid, total in totales.items():
        proxima = proximas.get(cid)
        salida[cid] = (
            f"cita próxima el {_local(proxima)}; {total} cita(s) en total" if proxima else f"{total} cita(s), ninguna próxima"
        )
    return salida


async def detalle(customer_id: uuid.UUID, limite: int) -> list[dict[str, Any]]:
    tid = get_current_tenant_id()
    async with get_session() as session:
        citas = (
            await session.execute(
                select(Appointment)
                .where(
                    Appointment.tenant_id == tid,
                    Appointment.customer_id == customer_id,
                    Appointment.deleted_at.is_(None),
                )
                .order_by(Appointment.start_at.desc())
                .limit(limite)
            )
        ).scalars().all()
    return [{"tipo": "cita", "titulo": c.title, "fecha": _local(c), "estado": c.status} for c in citas]
