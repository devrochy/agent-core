"""Sesiones del canal WhatsApp Web no oficial (`waweb`, ADR-0074).

Tabla de NEGOCIO (con `tenant_id` + RLS): la sesión multi-device de Neonize por
conexión, persistida CIFRADA para sobrevivir reinicios de contenedor. Una sola
sesión viva por conexión (índice único parcial por `deleted_at`). RLS con el
patrón `nullif` + FORCE de 0003/0034.

Revision ID: 0039
Revises: 0040
Create Date: 2026-09-27
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0039"
down_revision = "0040"
branch_labels = None
depends_on = None

_TABLE = "waweb_sessions"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("channel_connections.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("jid", sa.Text(), nullable=True),
        sa.Column("creds_encrypted", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending_qr"),
        sa.Column("last_qr_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("risk_ack_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("risk_ack_by", UUID(as_uuid=True), nullable=True),
        sa.Column("risk_ack_version", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_waweb_sessions_tenant", _TABLE, ["tenant_id", "created_at"])
    op.create_index(
        "uq_waweb_sessions_connection",
        _TABLE,
        ["tenant_id", "connection_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
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
    op.execute("drop index if exists uq_waweb_sessions_connection")
    op.execute("drop index if exists ix_waweb_sessions_tenant")
    op.drop_table(_TABLE)
