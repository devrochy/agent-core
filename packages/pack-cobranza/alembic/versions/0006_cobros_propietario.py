"""Cobros al propietario y links de pago (bloque E, Épica 9).

Revision ID: pack_cobranza_0006
Revises: pack_cobranza_0005
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "pack_cobranza_0006"
down_revision = "pack_cobranza_0005"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)
_UUID = UUID(as_uuid=True)
_NUM = sa.Numeric(14, 2)

_NEW_TABLES = ("pack_cobranza_cobros_propietario", "pack_cobranza_links_pago")


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )


def upgrade() -> None:
    op.create_table(
        "pack_cobranza_cobros_propietario",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("periodo", sa.Text(), nullable=False),
        sa.Column("monto_calculado", _NUM, nullable=False, server_default="0"),
        sa.Column("monto_pagado", _NUM, nullable=True),
        sa.Column("fecha_vencimiento", sa.Date(), nullable=False),
        sa.Column("fecha_pago", sa.Date(), nullable=True),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("metodo_pago", sa.Text(), nullable=True),
        sa.Column("registrado_por", sa.Text(), nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("uq_pack_cobranza_cobro_periodo", "pack_cobranza_cobros_propietario", ["tenant_id", "periodo"], unique=True)
    _rls("pack_cobranza_cobros_propietario")

    op.create_table(
        "pack_cobranza_links_pago",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("cobro_id", _UUID, nullable=True),
        sa.Column("referencia", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("monto", _NUM, nullable=False, server_default="0"),
        sa.Column("estado", sa.Text(), nullable=False, server_default="activo"),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_pack_cobranza_links_pago_tenant_cobro", "pack_cobranza_links_pago", ["tenant_id", "cobro_id"])
    op.create_index("uq_pack_cobranza_links_pago_referencia", "pack_cobranza_links_pago", ["tenant_id", "referencia"], unique=True)
    _rls("pack_cobranza_links_pago")

    for table in _NEW_TABLES:
        op.execute(f'alter table "{table}" force row level security')


def downgrade() -> None:
    for table in reversed(_NEW_TABLES):
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.drop_table(table)
