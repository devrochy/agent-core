"""Evals por versión de AgentSpec (ADR-0051).

Dos tablas de NEGOCIO (con `tenant_id` + RLS): `eval_runs` (una corrida del
golden set de un tenant, atada a `spec_version`) y `eval_results` (un escenario
por fila). El runner corre con un tenant real, así que aplica el aislamiento
estándar (invariante #1). RLS con el patrón `nullif` + FORCE de 0003/0034.

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-23
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_BUSINESS_TABLES = ("eval_runs", "eval_results")


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("spec_version", sa.Text(), nullable=True),
        sa.Column("git_ref", sa.Text(), nullable=True),
        sa.Column("passed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_eval_runs_tenant", "eval_runs", ["tenant_id", "created_at"])

    op.create_table(
        "eval_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scenario", sa.Text(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_eval_results_tenant", "eval_results", ["tenant_id", "run_id"])

    for table in _BUSINESS_TABLES:
        op.execute(f"alter table {table} enable row level security")
        op.execute(f"alter table {table} force row level security")
        op.execute(
            f"create policy tenant_isolation on {table} using "
            "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
            "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
        )
        op.execute(f"grant select, insert, update, delete on {table} to app_runtime")


def downgrade() -> None:
    for table in _BUSINESS_TABLES:
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.execute(f"alter table {table} no force row level security")
        op.execute(f"alter table {table} disable row level security")
    op.execute("drop index if exists ix_eval_results_tenant")
    op.drop_table("eval_results")
    op.execute("drop index if exists ix_eval_runs_tenant")
    op.drop_table("eval_runs")
