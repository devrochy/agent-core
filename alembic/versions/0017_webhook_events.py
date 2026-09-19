"""webhook_events: log crudo de entregas de webhook de Meta.

Tabla de SISTEMA (sin RLS, `tenant_id` nullable) -- excepción documentada a
la invariante nº1 de CLAUDE.md ("todo tabla de negocio lleva tenant_id not
null"). Un webhook puede llegar ANTES de poder resolver a qué tenant
pertenece (firma inválida, app todavía no vinculada) y este log existe
precisamente para auditar esos casos; exigir `tenant_id not null` lo
volvería inútil para su propio propósito. Misma categoría que `admins`/
`plugins` (ver docstring de core/db/models.py).

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("meta_connections.id", ondelete="SET NULL"), nullable=True),
        sa.Column("meta_app_id", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("signature_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("process_error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("meta_app_id", "external_id", name="uq_webhook_events_app_external"),
    )
    op.create_index(
        "ix_webhook_events_unprocessed",
        "webhook_events",
        ["received_at"],
        postgresql_where=sa.text("processed_at is null"),
    )
    op.execute("grant select, insert, update, delete on webhook_events to app_runtime")


def downgrade() -> None:
    op.execute("revoke all on webhook_events from app_runtime")
    op.execute("drop index if exists ix_webhook_events_unprocessed")
    op.drop_table("webhook_events")
