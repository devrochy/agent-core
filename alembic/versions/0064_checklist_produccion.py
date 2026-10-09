"""Checklist de producción por conexión y de la plataforma (HU-CONN-07).

- `meta_production_checks`: corridas del checklist de producción de UNA conexión de Meta
  (número registrado, WABA suscrita, webhook, plantillas mínimas aprobadas, token). Tabla de
  NEGOCIO: `tenant_id`, RLS habilitada y forzada, policy de aislamiento; una sola corrida
  activa (`queued`/`running`) por conexión (índice único parcial).
- `platform_readiness_runs`: corridas del checklist de la app de Meta de la plataforma y la
  infraestructura. Tabla de sistema (sin RLS, sin `tenant_id`): el resultado es de la
  plataforma y solo lo ve el master. Una sola corrida activa a la vez.

Las dos guardan el resultado con fecha; las escribe el job de Celery
(`core/tasks/production_readiness.py`), nunca el request.

Tablas nuevas y vacías: sin reescritura ni bloqueo largo. `purge_tenant` cubre
`meta_production_checks` sola (descubre las tablas con `tenant_id`).

Revision ID: 0064
Revises: 0062
Create Date: 2026-10-09
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0064"
down_revision = "0062"
branch_labels = None
depends_on = None

_ESTADOS = "status in ('queued','running','done','failed')"


def upgrade() -> None:
    op.create_table(
        "meta_production_checks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), sa.ForeignKey("meta_connections.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("ready", sa.Boolean(), nullable=True),
        sa.Column("checks", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_ESTADOS, name="ck_meta_production_checks_status"),
    )
    op.create_index(
        "ix_meta_production_checks_tenant_connection",
        "meta_production_checks",
        ["tenant_id", "connection_id", "requested_at"],
    )
    op.create_index(
        "uq_meta_production_checks_activa",
        "meta_production_checks",
        ["tenant_id", "connection_id"],
        unique=True,
        postgresql_where=sa.text("status in ('queued','running') and deleted_at is null"),
    )
    op.execute("ALTER TABLE meta_production_checks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE meta_production_checks FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY meta_production_checks_tenant_isolation ON meta_production_checks
        USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
        """
    )

    op.create_table(
        "platform_readiness_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("ready", sa.Boolean(), nullable=True),
        sa.Column("checks", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_ESTADOS, name="ck_platform_readiness_runs_status"),
    )
    op.create_index("ix_platform_readiness_runs_requested_at", "platform_readiness_runs", ["requested_at"])
    # Una sola corrida activa: índice único sobre una constante, parcial a las activas.
    op.execute(
        "CREATE UNIQUE INDEX uq_platform_readiness_runs_activa ON platform_readiness_runs ((true)) "
        "WHERE status in ('queued','running')"
    )

    # Explícito: los default privileges de 0003 solo valen si migra el mismo rol que la corrió.
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT SELECT, INSERT, UPDATE ON meta_production_checks TO app_runtime;
                GRANT SELECT, INSERT, UPDATE ON platform_readiness_runs TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("drop index if exists uq_platform_readiness_runs_activa")
    op.drop_index("ix_platform_readiness_runs_requested_at", table_name="platform_readiness_runs")
    op.drop_table("platform_readiness_runs")
    op.execute("drop policy if exists meta_production_checks_tenant_isolation on meta_production_checks")
    op.execute("alter table meta_production_checks no force row level security")
    op.execute("alter table meta_production_checks disable row level security")
    op.drop_index("uq_meta_production_checks_activa", table_name="meta_production_checks")
    op.drop_index("ix_meta_production_checks_tenant_connection", table_name="meta_production_checks")
    op.drop_table("meta_production_checks")
