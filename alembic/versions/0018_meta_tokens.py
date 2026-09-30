"""meta_private.meta_tokens: custodia cifrada (AES-256-GCM) de tokens de Meta.

Schema Postgres propio (`meta_private`, nativo de Postgres -- nada específico
de Supabase) para contener el radio de impacto: nada fuera de
`core/gateway/meta_token_store.py` debe tocar esta tabla (verificado por test
estático, no por permisos de BD -- este stack no tiene un rol "de la app"
separado de un rol "para servir directo al navegador": FastAPI es el único
que le habla a Postgres, siempre como `app_runtime`). RLS + policy
`tenant_isolation` normal (no deny-all: `app_runtime` necesita acceso
funcional, no hay un service-role distinto al que darle bypass). El schema
nuevo no hereda los grants por-default de `public`, así que se otorgan acá
explícitamente.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("create schema if not exists meta_private")
    op.execute("grant usage on schema meta_private to app_runtime")

    op.create_table(
        "meta_tokens",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("meta_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("iv", sa.LargeBinary(), nullable=False),
        sa.Column("auth_tag", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("connection_id", "kind", "subject_id", name="uq_meta_tokens_connection_kind_subject"),
        sa.CheckConstraint(
            "kind in ('user_access','system_user','page_access','long_lived_user',"
            "'app_access','business_integration_system_user')",
            name="ck_meta_tokens_kind",
        ),
        schema="meta_private",
    )
    op.create_index("ix_meta_tokens_tenant", "meta_tokens", ["tenant_id"], schema="meta_private")
    op.create_index(
        "ix_meta_tokens_expires_at",
        "meta_tokens",
        ["expires_at"],
        schema="meta_private",
        postgresql_where=sa.text("expires_at is not null and revoked_at is null"),
    )

    op.execute("alter table meta_private.meta_tokens enable row level security")
    op.execute(
        "create policy tenant_isolation on meta_private.meta_tokens using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute("grant select, insert, update, delete on meta_private.meta_tokens to app_runtime")


def downgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on meta_private.meta_tokens")
    op.execute("alter table meta_private.meta_tokens disable row level security")
    op.execute("drop index if exists meta_private.ix_meta_tokens_expires_at")
    op.execute("drop index if exists meta_private.ix_meta_tokens_tenant")
    op.drop_table("meta_tokens", schema="meta_private")
    op.execute("revoke usage on schema meta_private from app_runtime")
    op.execute("drop schema if exists meta_private")
