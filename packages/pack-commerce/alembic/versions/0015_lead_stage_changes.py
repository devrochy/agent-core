"""Historia de la etapa del lead y `stage_changed_at` (HU-CRM-18, SPEC-33, ADR-0181).

`pack_commerce_lead_stage_changes` es nueva: un registro por cada cambio de etapa (anterior,
nueva, actor agente|persona|sistema y cuándo), con `tenant_id`, RLS y clave de idempotencia
única por negocio. `pack_commerce_leads.stage_changed_at` es nullable y sin backfill: los leads
anteriores no inventan una fecha ni cambios pasados (la ficha usa su última actividad como
aproximación). No se agrega CHECK a `pack_commerce_leads.status`: hoy puede tener texto libre.

Aditiva: `add column` nullable sin default es solo metadato en Postgres y la tabla es nueva.

Revision ID: crm_0015
Revises: crm_0014
Create Date: 2026-10-30
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "crm_0015"
down_revision = "crm_0014"
branch_labels = None
depends_on = None

_TABLE = "pack_commerce_lead_stage_changes"


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(f"drop policy if exists tenant_isolation on {table}")
    op.execute(
        f"create policy tenant_isolation on {table} using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute(f"grant select, insert, update, delete on {table} to app_runtime")


def upgrade() -> None:
    # El ALTER pide un lock exclusivo breve en los leads: si una transacción larga lo tiene
    # tomado, fallar rápido en vez de encolar el tráfico detrás.
    op.execute("set local lock_timeout = '5s'")
    op.add_column("pack_commerce_leads", sa.Column("stage_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", UUID(as_uuid=True), nullable=True),
        sa.Column("from_stage", sa.Text(), nullable=False),
        sa.Column("to_stage", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "to_stage in ('nuevo','calificado','contactado','convertido','perdido')",
            name="ck_pack_commerce_lead_stage_changes_to_stage",
        ),
        sa.CheckConstraint(
            "actor in ('agente','persona','sistema')", name="ck_pack_commerce_lead_stage_changes_actor"
        ),
    )
    op.create_index(
        "uq_pack_commerce_lead_stage_changes_tenant_idem",
        _TABLE,
        ["tenant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key is not null"),
    )
    # Línea de tiempo de la ficha: los cambios del cliente, del más nuevo al más viejo.
    op.create_index(
        "ix_pack_commerce_lead_stage_changes_cliente", _TABLE, ["tenant_id", "customer_id", "changed_at", "id"]
    )
    _rls(_TABLE)


def downgrade() -> None:
    # Borra la historia de cambios de etapa y la columna `stage_changed_at`. Reversa solo si no hay
    # historia: quitarla con datos la perdería en silencio.
    conn = op.get_bind()
    # La tabla tiene RLS forzada: sin esto el conteo vería 0 filas.
    conn.execute(sa.text("set local row_security = off"))
    con_datos = conn.execute(sa.text(f"select count(*) from {_TABLE}")).scalar()
    if con_datos:
        raise RuntimeError("crm_0015: hay cambios de etapa registrados; la reversa borraría la historia.")
    op.execute("set local lock_timeout = '5s'")
    op.execute(f"drop policy if exists tenant_isolation on {_TABLE}")
    op.drop_index("ix_pack_commerce_lead_stage_changes_cliente", table_name=_TABLE)
    op.drop_index("uq_pack_commerce_lead_stage_changes_tenant_idem", table_name=_TABLE)
    op.drop_table(_TABLE)
    op.drop_column("pack_commerce_leads", "stage_changed_at")
