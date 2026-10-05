"""`mobile_audit_events`: auditoría de acciones críticas de la app (ADR-0097, HU-APP-15).

Tabla de NEGOCIO: tenant_id + RLS habilitada y forzada + policy de aislamiento.
Idempotente por (tenant_id, event_id): la app reenvía eventos offline sin
duplicar. `payload` guarda el detalle de la acción; `device_id` el equipo que la
ejecutó.

Revision ID: 0055
Revises: 0054
Create Date: 2026-10-05
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TABLE = "mobile_audit_events"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("admin_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", UUID(as_uuid=True), nullable=False),
        sa.Column("accion", sa.Text(), nullable=False),
        sa.Column("entidad", sa.Text(), nullable=False),
        sa.Column("entidad_id", UUID(as_uuid=True), nullable=True),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("device_id", sa.Text(), nullable=True),
        sa.Column("ocurrido_en", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        f"ix_{_TABLE}_admin", _TABLE, ["tenant_id", "admin_id"]
    )
    op.create_index(
        f"uq_{_TABLE}_tenant_event",
        _TABLE,
        ["tenant_id", "event_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(f"alter table {_TABLE} enable row level security")
    op.execute(f"alter table {_TABLE} force row level security")
    op.execute(
        f"create policy tenant_isolation on {_TABLE} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    op.execute(f"grant select, insert, update, delete on {_TABLE} to app_runtime")


def downgrade() -> None:
    op.execute(f"drop policy if exists tenant_isolation on {_TABLE}")
    op.execute(f"alter table {_TABLE} no force row level security")
    op.execute(f"alter table {_TABLE} disable row level security")
    op.drop_index(f"uq_{_TABLE}_tenant_event", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_admin", table_name=_TABLE)
    op.drop_table(_TABLE)
