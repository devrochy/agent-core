"""Tablas del CRM: crm_leads y crm_deals.

Revision ID: crm_0001
Revises:
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "crm_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "crm_leads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("interest", sa.Text(), nullable=False, server_default=""),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="nuevo"),
        sa.Column("idempotency_key", sa.Text(), nullable=True, unique=True),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_crm_leads_customer", "crm_leads", ["customer_id"])
    op.create_index("ix_crm_leads_status", "crm_leads", ["status"])

    op.create_table(
        "crm_deals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("lead_id", UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("stage", sa.Text(), nullable=False, server_default="propuesta"),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.Text(), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_crm_deals_lead", "crm_deals", ["lead_id"])


def downgrade() -> None:
    op.drop_table("crm_deals")
    op.drop_table("crm_leads")
