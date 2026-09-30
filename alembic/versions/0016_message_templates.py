"""message_templates: plantillas de WhatsApp sincronizadas desde Graph API.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_TEMPLATE_STATUSES = (
    "DRAFT", "PENDING", "APPROVED", "REJECTED", "PAUSED", "DISABLED", "IN_APPEAL",
    "ARCHIVED", "LOCKED", "LIMIT_EXCEEDED", "FLAGGED", "REINSTATED", "PENDING_DELETION", "DELETED",
)


def upgrade() -> None:
    op.create_table(
        "message_templates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("waba_account_id", UUID(as_uuid=True), sa.ForeignKey("waba_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("meta_template_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("parameter_format", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="DRAFT"),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("rejection_recommendation", sa.Text(), nullable=True),
        sa.Column("quality_score", sa.Text(), nullable=True),
        sa.Column("components", JSONB(), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("waba_account_id", "name", "language", name="uq_message_templates_waba_name_lang"),
        sa.CheckConstraint(
            "status in (" + ",".join(f"'{s}'" for s in _TEMPLATE_STATUSES) + ")",
            name="ck_message_templates_status",
        ),
    )
    op.create_index("ix_message_templates_tenant", "message_templates", ["tenant_id"])
    op.create_index(
        "uq_message_templates_meta_id",
        "message_templates",
        ["meta_template_id"],
        unique=True,
        postgresql_where=sa.text("meta_template_id is not null"),
    )

    op.execute("alter table message_templates enable row level security")
    op.execute(
        "create policy tenant_isolation on message_templates using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute("grant select, insert, update, delete on message_templates to app_runtime")


def downgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on message_templates")
    op.execute("alter table message_templates disable row level security")
    op.execute("drop index if exists uq_message_templates_meta_id")
    op.execute("drop index if exists ix_message_templates_tenant")
    op.drop_table("message_templates")
