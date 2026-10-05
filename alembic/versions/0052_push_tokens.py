"""`push_tokens`: dispositivos de push de la app oficial (ADR-0095, HU-APP-05).

Tabla de NEGOCIO: tenant_id + RLS habilitada y forzada + policy de aislamiento
(invariante 1). Un token es del negocio donde el admin lo registró; la baja es
soft (deleted_at) y la unicidad activa es parcial (tenant_id, token).

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-05
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TABLE = "push_tokens"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("admin_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False, server_default="expo"),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False, server_default="android"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(f"ix_{_TABLE}_admin", _TABLE, ["tenant_id", "admin_id"])
    op.create_index(
        f"uq_{_TABLE}_tenant_token",
        _TABLE,
        ["tenant_id", "token"],
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
    op.drop_index(f"uq_{_TABLE}_tenant_token", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_admin", table_name=_TABLE)
    op.drop_table(_TABLE)
