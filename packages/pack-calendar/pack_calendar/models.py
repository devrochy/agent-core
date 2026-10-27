"""Modelos de pack-calendar (invariante nº5: Base propia).

`customer_id`/`conversation_id` son FK lógicas (UUID sin constraint) al core.
Toda tabla lleva `tenant_id` (invariante nº1). Sin borrado físico (invariante nº8):
`deleted_at`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.shared.tenant import get_current_tenant_id


def utcnow() -> datetime:
    return datetime.now(UTC)


def _tenant_or_raise() -> uuid.UUID:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("INSERT pack_calendar_* sin tenant_scope")
    return tenant_id


class Base(DeclarativeBase):
    pass


# HU-UX-08: paleta categórica del sistema de diseño (SPEC-29), nunca hex libre.
COLORES_TIPO = ("cat-1", "cat-2", "cat-3", "cat-4")


class Calendar(Base):
    """Agenda del negocio (HU-CAL-08, ADR-0170): lo que el asistente le ofrece al cliente.

    Una conexión de Google = una agenda (`connection_id`, FK lógica a `channel_connections`);
    sin conexión es una agenda interna. `name` es el nombre que el asistente le dice al
    cliente. `timezone` y `business_hours` en `NULL` heredan los de `CalendarConfig`.
    """

    __tablename__ = "pack_calendar_calendars"
    __table_args__ = (
        Index("ix_pack_calendar_calendars_tenant", "tenant_id", "sort_order"),
        Index(
            "uq_pack_calendar_calendars_default",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_default and deleted_at is null"),
        ),
        Index(
            "uq_pack_calendar_calendars_connection",
            "tenant_id",
            "connection_id",
            unique=True,
            postgresql_where=text("connection_id is not null and deleted_at is null"),
        ),
        Index(
            "uq_pack_calendar_calendars_name",
            "tenant_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
        CheckConstraint(
            "color in ('cat-1', 'cat-2', 'cat-3', 'cat-4')", name="ck_pack_calendar_calendars_color"
        ),
        CheckConstraint("char_length(name) between 1 and 60", name="ck_pack_calendar_calendars_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=_tenant_or_raise
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    color: Mapped[str] = mapped_column(Text, nullable=False, default="cat-1", server_default="cat-1")
    timezone: Mapped[str | None] = mapped_column(Text)
    business_hours: Mapped[str | None] = mapped_column(Text)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    google_calendar_id: Mapped[str] = mapped_column(
        Text, nullable=False, default="primary", server_default="primary"
    )
    responsable_admin_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AppointmentType(Base):
    """Tipo de cita/servicio: nombre + duración propia."""

    __tablename__ = "pack_calendar_appointment_types"
    __table_args__ = (
        Index("ix_pack_calendar_types_tenant", "tenant_id", "created_at"),
        CheckConstraint(
            "color in ('cat-1', 'cat-2', 'cat-3', 'cat-4')", name="ck_pack_calendar_types_color"
        ),
        CheckConstraint("char_length(codigo) <= 6", name="ck_pack_calendar_types_codigo"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=_tenant_or_raise
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # HU-SH-02: colaborador que recibe los avisos de este tipo y enlace de la reunión.
    responsable_admin_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    enlace_reunion: Mapped[str | None] = mapped_column(Text)
    # HU-UX-08: color (clave de COLORES_TIPO) y código corto («VAL») en la Agenda.
    color: Mapped[str] = mapped_column(Text, nullable=False, default="cat-1", server_default="cat-1")
    codigo: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Appointment(Base):
    """Espejo local de una cita agendada en Google Calendar."""

    __tablename__ = "pack_calendar_appointments"
    __table_args__ = (
        Index("ix_pack_calendar_appts_tenant_start", "tenant_id", "start_at"),
        Index(
            "uq_pack_calendar_appt_google_event",
            "tenant_id",
            "google_event_id",
            unique=True,
            postgresql_where=text("google_event_id is not null and deleted_at is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=_tenant_or_raise
    )
    appointment_type_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    google_event_id: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attendee_email: Mapped[str | None] = mapped_column(Text)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="scheduled")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="assistant")
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="America/Bogota")
    asignado_admin_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # HU-SH-02
    # HU-CAL-08: agenda de la cita (FK lógica a `pack_calendar_calendars`). `NULL` = la
    # agenda por defecto, hasta la migración «contract» que lo vuelve NOT NULL.
    calendar_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CalendarBlock(Base):
    """Bloqueo puntual de la agenda interna (vacaciones, ausencia, feriado).

    Solo aplica al modo `interno` del pack (`CalendarConfig.source`), donde la
    disponibilidad se calcula contra los horarios de atención y los bloqueos
    (ADR-0084).
    """

    __tablename__ = "pack_calendar_blocks"
    __table_args__ = (
        Index("ix_pack_calendar_blocks_tenant_start", "tenant_id", "start_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=_tenant_or_raise
    )
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # HU-CAL-08: agenda a la que aplica el bloqueo. `NULL` = la agenda por defecto.
    calendar_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AppointmentEvent(Base):
    """Historial de acciones sobre una cita: programada / reprogramada / cancelada.

    Se escribe desde las tools del asistente y desde el panel (crear/mover/
    cancelar). `appointment_id` es FK lógica (sin constraint), igual que el resto
    del pack.
    """

    __tablename__ = "pack_calendar_appointment_events"
    __table_args__ = (
        Index("ix_pack_calendar_events_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=_tenant_or_raise
    )
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    old_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    new_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(Text, nullable=False, default="asistente")
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
