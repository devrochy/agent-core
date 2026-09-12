"""Salud de plugin por tenant (ADR-0024).

Tabla nueva `plugin_tenant_state`: sin fila activa (`deleted_at is null`) =
sano. Existe para que una excepción de runtime causada por el tráfico de
UN tenant no apague el plugin para todos los demás (incidente real
documentado en `docs/ROADMAP.md`, filas 143-149) -- hasta ahora
`auto_disable` solo podía escribir `PluginRecord.status`, una fila global
sin `tenant_id`. `deleted_at` (invariante #8: sin borrado físico) marca
"reactivado por un admin", no se hace DELETE real.

Nace vacía: sin backfill (nadie está "deshabilitado" hoy).

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-12
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_tenant_state",
        sa.Column("plugin_id", sa.Text(), sa.ForeignKey("plugins.id"), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="error"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_plugin_tenant_state_tenant", "plugin_tenant_state", ["tenant_id"])

    # RLS + policy (mismo patrón nullif de 0003/0004)
    op.execute("alter table plugin_tenant_state enable row level security")
    op.execute(
        "create policy tenant_isolation on plugin_tenant_state using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute("grant select, insert, update, delete on plugin_tenant_state to app_runtime")


def downgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on plugin_tenant_state")
    op.execute("alter table plugin_tenant_state disable row level security")
    op.execute("drop index if exists ix_plugin_tenant_state_tenant")
    op.drop_table("plugin_tenant_state")
