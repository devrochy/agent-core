"""Outbox `domain_events` (HU-SUB-03, ADR-0107).

Los eventos de dominio (`payment.*` hoy; `order.*`/`subscription.*` después)
se escriben en la MISMA transacción que su cambio de estado y los entrega el
despachador de Celery con reintentos. La escritura es idempotente por
`dedupe_key`; la entrega es at-least-once y los handlers deben ser
idempotentes por su cuenta.

Revision ID: 0056
Revises: 0055
Create Date: 2026-10-07
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "domain_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pendiente",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_domain_events_tenant_status_next",
        "domain_events",
        ["tenant_id", "status", "next_attempt_at"],
    )
    op.create_index(
        "uq_domain_events_tenant_dedupe",
        "domain_events",
        ["tenant_id", "dedupe_key"],
        unique=True,
    )
    op.execute("ALTER TABLE domain_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE domain_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY domain_events_tenant_isolation ON domain_events
        USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION admin_domain_events_due(p_limit integer, p_before timestamptz)
        RETURNS TABLE(id uuid, tenant_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT e.id, e.tenant_id
            FROM domain_events e
            WHERE e.status = 'pendiente' AND e.next_attempt_at <= p_before
            ORDER BY e.next_attempt_at
            LIMIT p_limit;
        $$;

        REVOKE ALL ON FUNCTION admin_domain_events_due(integer, timestamptz) FROM PUBLIC;
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT EXECUTE ON FUNCTION admin_domain_events_due(integer, timestamptz) TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("drop function if exists admin_domain_events_due(integer, timestamptz)")
    op.execute("drop table if exists domain_events")
