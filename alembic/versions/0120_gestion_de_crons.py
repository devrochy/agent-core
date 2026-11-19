"""Gestión de crons: estado operativo e historial de ejecuciones (ADR-0183).

- `cron_job_configs`: un cron pausado y/o con otro horario que el del catálogo. Sin fila = valores
  por defecto. La escribe la API de gestión (master) y la lee el wrapper de cada cron y el
  planificador del beat.
- `cron_runs`: cada ejecución de un cron (programada o manual) con su estado y duración. De un
  error guarda solo la clase de la excepción.

Tabla de SISTEMA (sin RLS, sin `tenant_id`): los crons son de la plataforma, recorren los
negocios por dentro y estas filas no contienen datos de ningún negocio. Mismo criterio que
`platform_readiness_runs` (0064). Solo las toca `core/admin/crons_service.py`.

Tablas nuevas y vacías: sin reescritura ni bloqueo largo. `cron_runs` se poda a los 30 días
(`core.cron_runs_retention`), por eso `app_runtime` recibe DELETE ahí y solo ahí.

Revision ID: 0120
Revises: 0119
Create Date: 2026-11-02
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cron_job_configs",
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("schedule_override", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("task_name"),
    )

    op.create_table(
        "cron_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("trigger", sa.Text(), nullable=False, server_default="scheduled"),
        sa.Column("triggered_by", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status in ('running','success','failed','skipped')", name="ck_cron_runs_status"),
        sa.CheckConstraint("trigger in ('scheduled','manual')", name="ck_cron_runs_trigger"),
    )
    op.execute("CREATE INDEX ix_cron_runs_task_started ON cron_runs (task_name, started_at DESC)")
    op.create_index("ix_cron_runs_started_at", "cron_runs", ["started_at"])

    # Explícito: los default privileges de 0003 solo valen si migra el mismo rol que la corrió.
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT SELECT, INSERT, UPDATE ON cron_job_configs TO app_runtime;
                GRANT SELECT, INSERT, UPDATE, DELETE ON cron_runs TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_cron_runs_started_at", table_name="cron_runs")
    op.execute("drop index if exists ix_cron_runs_task_started")
    op.drop_table("cron_runs")
    op.drop_table("cron_job_configs")
