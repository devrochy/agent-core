"""Contribución del pack a la vista global de leads (ADR-0056, HU-CRM-04 y tanda 4).

Aporta las columnas de Agenda (próxima cita, última cita, conteos y tipo frecuente) y las
citas del cliente en su línea de tiempo. No edita datos del cliente: las citas se gestionan
en Agenda; el contacto es del perfil del core (ADR-0153).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, select

from core.admin.lead_registry import (
    Cursor,
    LeadColumn,
    LeadRelacion,
    TimelineEvent,
    antes_del_cursor,
    lead_registry,
)
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id
from pack_calendar import datos as _datos  # noqa: F401  (acciones de la política de datos)
from pack_calendar.models import Appointment, AppointmentType

PLUGIN_ID = "pack_calendar"
_ESTADO = {"scheduled": "agendada", "cancelled": "cancelada", "completed": "atendida", "no_show": "no asistió"}


def _fecha(valor: datetime | None, estado: str | None = None) -> str | None:
    if valor is None:
        return None
    texto = valor.strftime("%Y-%m-%d %H:%M")
    return f"{texto} · {_ESTADO.get(estado or '', estado)}" if estado else texto


async def _resolver(customer_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not customer_ids:
        return {}
    tid = get_current_tenant_id()
    ahora = datetime.now(UTC)
    async with get_session() as session:
        citas = (
            (
                await session.execute(
                    select(Appointment).where(
                        Appointment.tenant_id == tid,
                        Appointment.customer_id.in_(customer_ids),
                        Appointment.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        tipos = dict(
            (
                await session.execute(
                    select(AppointmentType.id, AppointmentType.name).where(
                        AppointmentType.tenant_id == tid,
                        AppointmentType.deleted_at.is_(None),
                    )
                )
            ).all()
        )
    por_cliente: dict[uuid.UUID, list[Appointment]] = {}
    for cita in citas:
        if cita.customer_id is not None:
            por_cliente.setdefault(cita.customer_id, []).append(cita)

    out: dict[uuid.UUID, dict[str, Any]] = {}
    for customer_id in customer_ids:
        propias = por_cliente.get(customer_id, [])
        futuras = [c for c in propias if c.status == "scheduled" and c.start_at >= ahora]
        pasadas = [c for c in propias if c.start_at < ahora]
        proxima = min(futuras, key=lambda c: (c.start_at, str(c.id)), default=None)
        ultima = max(pasadas, key=lambda c: (c.start_at, str(c.id)), default=None)
        conteo: dict[str, int] = {}
        for cita in propias:
            if cita.appointment_type_id is not None:
                conteo[str(cita.appointment_type_id)] = conteo.get(str(cita.appointment_type_id), 0) + 1
        tipo_id = max(conteo, key=conteo.get, default=None) if conteo else None
        out[customer_id] = {
            "proxima_cita": _fecha(proxima.start_at, proxima.status) if proxima else None,
            "ultima_cita": _fecha(ultima.start_at, ultima.status) if ultima else None,
            "citas": len(propias) or None,
            "canceladas": sum(1 for c in propias if c.status == "cancelled") or None,
            "no_asistio": sum(1 for c in propias if c.status == "no_show") or None,
            "tipo_frecuente": tipos.get(uuid.UUID(tipo_id)) if tipo_id else None,
        }
    return out


async def _con_datos(ids: list[uuid.UUID] | None) -> Select[Any]:
    """Clientes con alguna cita no borrada (agendada, atendida, cancelada o no asistió)."""
    q = select(Appointment.customer_id).where(
        Appointment.tenant_id == get_current_tenant_id(),
        Appointment.customer_id.is_not(None),
        Appointment.deleted_at.is_(None),
    )
    if ids is not None:
        q = q.where(Appointment.customer_id.in_(ids))
    return q.distinct()


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


lead_registry.register(
    PLUGIN_ID,
    columns=[
        LeadColumn(key="proxima_cita", label="Próxima cita"),
        LeadColumn(key="ultima_cita", label="Última cita"),
        LeadColumn(key="citas", label="Citas"),
        LeadColumn(key="canceladas", label="Canceladas"),
        LeadColumn(key="no_asistio", label="No asistió"),
        LeadColumn(key="tipo_frecuente", label="Tipo frecuente"),
    ],
    resolver=_resolver,
    timeline=_timeline,
    agent_key="agenda",
    agent_name="Agenda",
    icon="calendar",
    relacion=LeadRelacion(con_datos=_con_datos),
)


# Registra también los KPIs del módulo para el Resumen por agente (ADR-0171).
from pack_calendar import resumen as _resumen  # noqa: E402, F401
