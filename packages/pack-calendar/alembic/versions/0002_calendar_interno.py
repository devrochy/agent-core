"""Calendario interno: bloqueos de agenda + asegura las tablas del pack (ADR-0084).

`pack_calendar_blocks` es nueva (bloqueos puntuales de la agenda interna: vacaciones,
ausencia, feriado). Además se asegura `pack_calendar_appointment_events` — el modelo
existe desde 0001 pero la tabla no se creaba en instalaciones nuevas (solo se había
creado a mano en algunos entornos): `create table if not exists` la deja siempre lista.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_TABLES = ("pack_calendar_blocks", "pack_calendar_appointment_events")


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(f"drop policy if exists tenant_isolation on {table}")
    op.execute(
        f"create policy tenant_isolation on {table} using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute(f"grant select, insert, update, delete on {table} to app_runtime")


def upgrade() -> None:
    op.create_table(
        "pack_calendar_blocks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_pack_calendar_blocks_tenant_start",
        "pack_calendar_blocks",
        ["tenant_id", "start_at"],
    )

    # Historial de citas: el modelo existe desde 0001, la tabla no se creaba.
    op.execute(
        """
        create table if not exists pack_calendar_appointment_events (
            id uuid primary key,
            tenant_id uuid not null,
            appointment_id uuid,
            action text not null,
            title text not null default '',
            old_start_at timestamptz,
            new_start_at timestamptz,
            actor text not null default 'asistente',
            conversation_id uuid,
            deleted_at timestamptz,
            created_at timestamptz not null default now()
        )
        """
    )
    op.execute(
        "create index if not exists ix_pack_calendar_events_tenant_created "
        "on pack_calendar_appointment_events (tenant_id, created_at)"
    )

    for table in _TABLES:
        _rls(table)


def downgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on pack_calendar_blocks")
    op.execute("alter table pack_calendar_blocks no force row level security")
    op.execute("alter table pack_calendar_blocks disable row level security")
    op.drop_index("ix_pack_calendar_blocks_tenant_start", table_name="pack_calendar_blocks")
    op.drop_table("pack_calendar_blocks")
