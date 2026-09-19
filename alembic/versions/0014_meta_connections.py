"""meta_connections: reemplaza app_settings.meta_app_config_encrypted (0013).

Metadata de la app de Meta conectada por tenant (status, scopes, salud) --
el App Secret/tokens de la app viven cifrados aparte, en `meta_private.
meta_tokens` (migración 0018), no acá. `meta_app_config_encrypted` (blob
simple agregado en 0013) queda deprecado: se dropea sin backfill porque la
feature recién se publicó y no hay datos reales todavía.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("app_settings", "meta_app_config_encrypted")

    op.create_table(
        "meta_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False, server_default="production"),
        sa.Column("meta_app_id", sa.Text(), nullable=False),
        sa.Column("meta_config_id", sa.Text(), nullable=True),
        sa.Column("business_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("graph_api_version", sa.Text(), nullable=False, server_default="v26.0"),
        sa.Column("scopes", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("connected_by", UUID(as_uuid=True), sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("health", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "tenant_id", "channel", "environment", "meta_app_id", name="uq_meta_connections_tenant_app"
        ),
        sa.CheckConstraint(
            "channel in ('whatsapp','messenger','instagram','facebook_page')",
            name="ck_meta_connections_channel",
        ),
        sa.CheckConstraint("environment in ('sandbox','production')", name="ck_meta_connections_environment"),
        sa.CheckConstraint(
            "status in ('draft','pending','connected','degraded','revoked','error')",
            name="ck_meta_connections_status",
        ),
    )
    op.create_index("ix_meta_connections_tenant", "meta_connections", ["tenant_id"])

    op.execute("alter table meta_connections enable row level security")
    op.execute(
        "create policy tenant_isolation on meta_connections using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute("grant select, insert, update, delete on meta_connections to app_runtime")


def downgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on meta_connections")
    op.execute("alter table meta_connections disable row level security")
    op.execute("drop index if exists ix_meta_connections_tenant")
    op.drop_table("meta_connections")
    op.add_column("app_settings", sa.Column("meta_app_config_encrypted", sa.Text(), nullable=True))
