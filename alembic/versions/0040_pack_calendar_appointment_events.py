"""Historial de acciones de las citas de pack-calendar.

Tabla de NEGOCIO (con `tenant_id` + RLS): `pack_calendar_appointment_events`
registra cada acción sobre una cita (programada / reprogramada / cancelada),
escrita tanto por las tools del asistente como por el panel. Alimenta el botón
"Actividades" de la vista de Citas.

Revision ID: 0040
Revises: 0038
Create Date: 2026-09-28
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0040"
down_revision = "0038"
branch_labels = None
depends_on = None

_TABLE = "pack_calendar_appointment_events"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("appointment_id", UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("old_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False, server_default="asistente"),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_pack_calendar_events_tenant_created", _TABLE, ["tenant_id", "created_at"]
    )
    op.execute(f"alter table {_TABLE} enable row level security")
    op.execute(f"alter table {_TABLE} force row level security")
    op.execute(
        f"create policy tenant_isolation on {_TABLE} using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute(f"grant select, insert, update, delete on {_TABLE} to app_runtime")


def downgrade() -> None:
    op.execute(f"drop policy if exists tenant_isolation on {_TABLE}")
    op.execute(f"alter table {_TABLE} no force row level security")
    op.execute(f"alter table {_TABLE} disable row level security")
    op.execute("drop index if exists ix_pack_calendar_events_tenant_created")
    op.drop_table(_TABLE)