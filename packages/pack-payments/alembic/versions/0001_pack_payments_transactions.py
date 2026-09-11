"""pack_payments_transactions: tabla inicial del pack de pagos (ADR-0023).

Tabla nueva (greenfield, sin datos previos que migrar): tenant_id NOT NULL
desde el arranque, RLS + policy de aislamiento, e índices compuestos que
empiezan por tenant_id (invariante nº1 de CLAUDE.md).

Revision ID: pack_payments_0001
Revises:
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "pack_payments_0001"
down_revision = None
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    op.create_table(
        "pack_payments_transactions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False, server_default="wompi"),
        sa.Column("provider_reference", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.Text(), nullable=False, server_default="COP"),
        sa.Column("payment_link_url", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("last_webhook_payload", JSONB(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )
    op.create_index(
        "uq_pack_payments_tenant_reference",
        "pack_payments_transactions",
        ["tenant_id", "reference"],
        unique=True,
    )
    op.create_index(
        "uq_pack_payments_tenant_idem",
        "pack_payments_transactions",
        ["tenant_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_pack_payments_tenant_provider_ref",
        "pack_payments_transactions",
        ["tenant_id", "provider", "provider_reference"],
    )

    op.execute("alter table pack_payments_transactions enable row level security")
    op.execute(
        f"create policy tenant_isolation on pack_payments_transactions "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )


def downgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on pack_payments_transactions")
    op.drop_table("pack_payments_transactions")
