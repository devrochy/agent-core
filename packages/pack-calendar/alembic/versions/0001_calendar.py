"""Tablas de pack-calendar: tipos de cita y citas (ADR-0069/0070).

Revision ID: 0001
Revises:
Create Date: 2026-09-25
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_TABLES = ("pack_calendar_appointment_types", "pack_calendar_appointments")


def upgrade() -> None:
    op.create_table(
        "pack_calendar_appointment_types",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_pack_calendar_types_tenant",
        "pack_calendar_appointment_types",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "pack_calendar_appointments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("appointment_type_id", UUID(as_uuid=True), nullable=True),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("google_event_id", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("attendee_email", sa.Text(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="scheduled"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_pack_calendar_appts_tenant_start",
        "pack_calendar_appointments",
        ["tenant_id", "start_at"],
    )
    op.execute(
        "create unique index uq_pack_calendar_appt_google_event on pack_calendar_appointments "
        "(tenant_id, google_event_id) where google_event_id is not null and deleted_at is null"
    )

    # RLS + policy (mismo patrón nullif de 0003) + FORCE (como pack-cobranza 0003).
    for table in _TABLES:
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"alter table {table} force row level security")
        op.execute(
            f"create policy tenant_isolation on {table} using "
            "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
            "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
        )
        op.execute(f"grant select, insert, update, delete on {table} to app_runtime")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.execute(f"alter table {table} no force row level security")
        op.execute(f"alter table {table} disable row level security")
    op.execute("drop index if exists uq_pack_calendar_appt_google_event")
    op.drop_index("ix_pack_calendar_appts_tenant_start", table_name="pack_calendar_appointments")
    op.drop_table("pack_calendar_appointments")
    op.drop_index("ix_pack_calendar_types_tenant", table_name="pack_calendar_appointment_types")
    op.drop_table("pack_calendar_appointment_types")
