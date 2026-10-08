"""Cambio de plan, referencia del pedido y cargos por tipo (HU-SUB-07, ADR-0111).

- `pack_subscriptions_charges.tipo`: `periodo` (el de siempre) o `cambio_plan` (diferencia
  prorrateada de un upgrade). Default `periodo`: las filas existentes quedan como período.
- `pack_subscriptions_charges.cambio_plan` (JSONB): el plan al que lleva el cargo de cambio.
- `pack_subscriptions_subscriptions.referencia_origen`: número público del pedido que la
  creó; índice único parcial por negocio y variante (el mismo pedido no crea dos
  suscripciones del mismo plan; un pedido con dos planes crea dos).
- `pack_subscriptions_subscriptions.cambio_programado` (JSONB): downgrade al próximo período.

Columnas nullable o con default constante: sin reescritura de tabla ni bloqueo largo. Las
tablas ya tienen `tenant_id` + RLS forzada; el índice nuevo empieza por `tenant_id`.

Revision ID: pack_subscriptions_0006
Revises: pack_subscriptions_0005
Create Date: 2026-10-08
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "pack_subscriptions_0006"
down_revision = "pack_subscriptions_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pack_subscriptions_charges",
        sa.Column("tipo", sa.Text(), nullable=False, server_default=sa.text("'periodo'")),
    )
    op.add_column("pack_subscriptions_charges", sa.Column("cambio_plan", postgresql.JSONB(), nullable=True))
    op.create_check_constraint(
        "ck_pack_subscriptions_charges_tipo",
        "pack_subscriptions_charges",
        "tipo in ('periodo', 'cambio_plan')",
    )
    op.add_column("pack_subscriptions_subscriptions", sa.Column("referencia_origen", sa.Text(), nullable=True))
    op.add_column("pack_subscriptions_subscriptions", sa.Column("cambio_programado", postgresql.JSONB(), nullable=True))
    op.create_index(
        "uq_pack_subscriptions_subs_tenant_referencia",
        "pack_subscriptions_subscriptions",
        ["tenant_id", "referencia_origen", "variante_id"],
        unique=True,
        postgresql_where=sa.text("referencia_origen is not null and deleted_at is null"),
    )


def downgrade() -> None:
    op.drop_index("uq_pack_subscriptions_subs_tenant_referencia", table_name="pack_subscriptions_subscriptions")
    op.drop_column("pack_subscriptions_subscriptions", "cambio_programado")
    op.drop_column("pack_subscriptions_subscriptions", "referencia_origen")
    op.drop_constraint("ck_pack_subscriptions_charges_tipo", "pack_subscriptions_charges", type_="check")
    op.drop_column("pack_subscriptions_charges", "cambio_plan")
    op.drop_column("pack_subscriptions_charges", "tipo")
