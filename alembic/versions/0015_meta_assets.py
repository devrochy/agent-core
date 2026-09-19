"""Activos de Meta sincronizados desde Graph API: waba_accounts, phone_numbers,
fb_pages, ig_accounts. `tenant_id not null` en las 4, aunque sea derivable por
join a `meta_connections` -- invariante nº1 de CLAUDE.md, y así RLS/queries no
dependen de un join para aislar por tenant.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_TABLES = ("waba_accounts", "phone_numbers", "fb_pages", "ig_accounts")


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
        "waba_accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("meta_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("waba_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("timezone_id", sa.Text(), nullable=True),
        sa.Column("message_template_namespace", sa.Text(), nullable=True),
        sa.Column("account_review_status", sa.Text(), nullable=True),
        sa.Column("subscribed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("waba_id", "connection_id", name="uq_waba_accounts_waba_connection"),
    )

    op.create_table(
        "phone_numbers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("meta_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("waba_account_id", UUID(as_uuid=True), sa.ForeignKey("waba_accounts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("phone_number_id", sa.Text(), nullable=False, unique=True),
        sa.Column("display_phone_number", sa.Text(), nullable=True),
        sa.Column("verified_name", sa.Text(), nullable=True),
        sa.Column("quality_rating", sa.Text(), nullable=True),
        sa.Column("messaging_limit_tier", sa.Text(), nullable=True),
        sa.Column("name_status", sa.Text(), nullable=True),
        sa.Column("code_verification_status", sa.Text(), nullable=True),
        sa.Column("platform_type", sa.Text(), nullable=True),
        sa.Column("registered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pin_set", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("raw", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "fb_pages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("meta_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("subscribed_fields", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("raw", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("page_id", "connection_id", name="uq_fb_pages_page_connection"),
    )

    op.create_table(
        "ig_accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("meta_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ig_user_id", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("linked_page_id", sa.Text(), nullable=True),
        sa.Column("login_route", sa.Text(), nullable=True),
        sa.Column("raw", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("ig_user_id", "connection_id", name="uq_ig_accounts_iguser_connection"),
        sa.CheckConstraint(
            "login_route is null or login_route in ('ig_facebook_login','ig_instagram_login')",
            name="ck_ig_accounts_login_route",
        ),
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
