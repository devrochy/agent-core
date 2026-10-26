"""Centro de notificaciones: `notifications`, `notification_state`, `notification_reads` (ADR-0167).

Tres tablas de NEGOCIO: tenant_id + RLS habilitada y forzada + policy de aislamiento
(invariante 1), baja soft (`deleted_at`, invariante 8) y grant a `app_runtime`.

- `notifications`: una fila por evento (no por destinatario). Único parcial
  `(tenant_id, dedupe_key)` para que un evento repetido no duplique el aviso.
- `notification_state`: una fila por `(tenant_id, admin_id)`, sin sembrar: la marca de
  «leído hasta», el silencio y las categorías con el push apagado.
- `notification_reads`: solo existen filas de lo leído o archivado por cada admin.

Revision ID: 0107
Revises: 0106
Create Date: 2026-10-25
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from alembic import op

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TABLES = ("notifications", "notification_state", "notification_reads")


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    op.execute(f"grant select, insert, update, delete on {table} to app_runtime")


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("data", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("target", JSONB(), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("push_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "category in ('conversaciones','ventas','cobros','agenda','sistema')",
            name="ck_notifications_category",
        ),
        sa.CheckConstraint(
            "severity in ('info','success','warning','critical')",
            name="ck_notifications_severity",
        ),
    )
    op.create_index(
        "uq_notifications_tenant_dedupe",
        "notifications",
        ["tenant_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_notifications_tenant_created",
        "notifications",
        ["tenant_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "notification_state",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("admin_id", UUID(as_uuid=True), nullable=False),
        sa.Column("read_all_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "push_off_categories",
            ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "uq_notification_state_admin",
        "notification_state",
        ["tenant_id", "admin_id"],
        unique=True,
    )

    op.create_table(
        "notification_reads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("admin_id", UUID(as_uuid=True), nullable=False),
        sa.Column("notification_id", UUID(as_uuid=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "uq_notification_reads_admin_notif",
        "notification_reads",
        ["tenant_id", "admin_id", "notification_id"],
        unique=True,
    )

    for table in _TABLES:
        _rls(table)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.execute(f"alter table {table} no force row level security")
        op.execute(f"alter table {table} disable row level security")
    op.drop_index("uq_notification_reads_admin_notif", table_name="notification_reads")
    op.drop_table("notification_reads")
    op.drop_index("uq_notification_state_admin", table_name="notification_state")
    op.drop_table("notification_state")
    op.drop_index("ix_notifications_tenant_created", table_name="notifications")
    op.drop_index("uq_notifications_tenant_dedupe", table_name="notifications")
    op.drop_table("notifications")
