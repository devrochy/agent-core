"""Sin doble reserva, responsable y enlace por tipo, asignado de la cita (HU-SH-02).

- Restricción de exclusión `ex_pack_calendar_appointments_sin_solape`: dos citas
  programadas de un negocio no pueden solaparse (agenda interna: `google_event_id is
  null`; en modo google manda Google). La garantiza la base aunque dos reservas
  lleguen a la vez (antes: consultar y luego insertar, en sesiones separadas).
  Necesita `btree_gist` (extensión de confianza desde PG13).
- `pack_calendar_appointment_types.responsable_admin_id` y `.enlace_reunion`.
- `pack_calendar_appointments.asignado_admin_id` (lo hereda del tipo al crearse).

Columnas nullable: sin reescritura. Las tablas ya tienen `tenant_id` + RLS forzada; la
restricción empieza por `tenant_id`. Antes de aplicar en producción, verificar que no
haya citas internas solapadas (la creación de la restricción fallaría).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("create extension if not exists btree_gist")
    op.execute(
        """
        alter table pack_calendar_appointments
        add constraint ex_pack_calendar_appointments_sin_solape
        exclude using gist (tenant_id with =, tstzrange(start_at, end_at) with &&)
        where (status = 'scheduled' and deleted_at is null and google_event_id is null)
        """
    )
    op.add_column(
        "pack_calendar_appointment_types",
        sa.Column("responsable_admin_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("pack_calendar_appointment_types", sa.Column("enlace_reunion", sa.Text(), nullable=True))
    op.add_column(
        "pack_calendar_appointments",
        sa.Column("asignado_admin_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pack_calendar_appointments", "asignado_admin_id")
    op.drop_column("pack_calendar_appointment_types", "enlace_reunion")
    op.drop_column("pack_calendar_appointment_types", "responsable_admin_id")
    op.execute(
        "alter table pack_calendar_appointments drop constraint if exists ex_pack_calendar_appointments_sin_solape"
    )
