"""Preflight de integración Meta: config de app por tenant + checklist manual.

`app_settings.meta_app_config_encrypted`: blob cifrado (Fernet, mismo
`get_secret_box()` que `channel_connections.credentials_encrypted`) con
`{app_id, app_secret, config_id}` -- la app de Meta del tenant (BYOK), NULL
hasta que el tenant la configure (el preflight cae a fallback de .env).

`meta_setup_checklist`: un paso manual (crear la app, copiar App Secret,
añadir productos, etc.) no automatizable, marcado por un admin desde el
panel. Mismo patrón RLS/policy de 0012.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-19
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("meta_app_config_encrypted", sa.Text(), nullable=True))

    op.create_table(
        "meta_setup_checklist",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("step_key", sa.Text(), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("done_by", UUID(as_uuid=True), sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "step_key", name="uq_meta_checklist_tenant_step"),
    )
    op.create_index("ix_meta_setup_checklist_tenant", "meta_setup_checklist", ["tenant_id"])

    # RLS + policy (mismo patrón nullif de 0003/0012)
    op.execute("alter table meta_setup_checklist enable row level security")
    op.execute(
        "create policy tenant_isolation on meta_setup_checklist using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute("grant select, insert, update, delete on meta_setup_checklist to app_runtime")


def downgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on meta_setup_checklist")
    op.execute("alter table meta_setup_checklist disable row level security")
    op.execute("drop index if exists ix_meta_setup_checklist_tenant")
    op.drop_table("meta_setup_checklist")
    op.drop_column("app_settings", "meta_app_config_encrypted")
