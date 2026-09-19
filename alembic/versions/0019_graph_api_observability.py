"""graph_api_calls / graph_api_usage: observabilidad de llamadas a Graph API.

Versión mínima (el propio ticket original la marca como "fase 2") --
ampliable después sin romper nada: una fila por llamada + un agregado diario.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_TABLES = ("graph_api_calls", "graph_api_usage")


def _enable_rls(table: str) -> None:
    op.create_index(f"ix_{table}_tenant", table, ["tenant_id"])
    op.execute(f"alter table {table} enable row level security")
    op.execute(
        f"create policy tenant_isolation on {table} using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute(f"grant select, insert, update, delete on {table} to app_runtime")


def upgrade() -> None:
    op.create_table(
        "graph_api_calls",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("meta_connections.id", ondelete="SET NULL"), nullable=True),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("called_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "graph_api_usage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("meta_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("usage_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("connection_id", "usage_date", name="uq_graph_api_usage_connection_date"),
    )

    for table in _TABLES:
        _enable_rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.execute(f"alter table {table} disable row level security")
        op.execute(f"drop index if exists ix_{table}_tenant")
    for table in reversed(_TABLES):
        op.drop_table(table)
