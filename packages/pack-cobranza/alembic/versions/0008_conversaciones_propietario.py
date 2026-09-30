"""Conversaciones con el propietario (Épica 9, HU-63).

Revision ID: pack_cobranza_0008
Revises: pack_cobranza_0007
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "pack_cobranza_0008"
down_revision = "pack_cobranza_0007"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)
_UUID = UUID(as_uuid=True)

_NEW_TABLES = ("pack_cobranza_conversaciones_propietario", "pack_cobranza_mensajes_propietario")


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )


def upgrade() -> None:
    op.create_table(
        "pack_cobranza_conversaciones_propietario",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("asunto", sa.Text(), nullable=False, server_default="Comunicación con el propietario"),
        sa.Column("estado", sa.Text(), nullable=False, server_default="abierta"),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "uq_pack_cobranza_conversacion_propietario",
        "pack_cobranza_conversaciones_propietario",
        ["tenant_id"],
        unique=True,
    )
    _rls("pack_cobranza_conversaciones_propietario")

    op.create_table(
        "pack_cobranza_mensajes_propietario",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("conversacion_id", _UUID, nullable=False),
        sa.Column("direccion", sa.Text(), nullable=False, server_default="saliente"),
        sa.Column("tipo", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("autor_rol", sa.Text(), nullable=True),
        sa.Column("contenido", sa.Text(), nullable=False),
        sa.Column("cobro_id", _UUID, nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_pack_cobranza_mensajes_propietario_tenant_conversacion",
        "pack_cobranza_mensajes_propietario",
        ["tenant_id", "conversacion_id"],
    )
    _rls("pack_cobranza_mensajes_propietario")

    for table in _NEW_TABLES:
        op.execute(f'alter table "{table}" force row level security')


def downgrade() -> None:
    for table in reversed(_NEW_TABLES):
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.drop_table(table)
