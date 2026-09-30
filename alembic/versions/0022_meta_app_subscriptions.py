"""meta_app_subscriptions: estado de la suscripción de webhook a nivel de
APP de Meta (POST /{app_id}/subscriptions), distinto de `webhook_events`
(log por entrega) y de `WabaAccount.subscribed`/`FbPage.subscribed_fields`
(suscripción por objeto individual).

Tabla de SISTEMA (sin RLS, sin columna `tenant_id`): una misma app de Meta
puede ser usada por varios tenants (BYOK), así que este recurso vive a nivel
de app, no de tenant -- ni siquiera aplica un `tenant_id` nullable como en
`webhook_events` (ver docs/adr/0029-meta-app-subscriptions-tabla-sistema.md).

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-21
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meta_app_subscriptions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("meta_app_id", sa.Text(), nullable=False),
        sa.Column("object", sa.Text(), nullable=False),
        sa.Column("callback_url", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=True),
        sa.Column("fields", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "object in ('whatsapp_business_account','page','instagram')",
            name="ck_meta_app_subscriptions_object",
        ),
        sa.UniqueConstraint("meta_app_id", "object", name="uq_meta_app_subscriptions_app_object"),
    )
    op.execute("grant select, insert, update, delete on meta_app_subscriptions to app_runtime")


def downgrade() -> None:
    op.execute("revoke all on meta_app_subscriptions from app_runtime")
    op.drop_table("meta_app_subscriptions")
