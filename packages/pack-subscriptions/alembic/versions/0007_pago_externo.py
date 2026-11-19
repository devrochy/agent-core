"""Pago de un cargo registrado por otro canal (HU-SH-04, ADR-0123).

- `pack_subscriptions_charges.pago_externo` (JSONB): el cargo lo pagó otro canal, no el link
  del pack (`{"canal", "referencia", "registrado_en"}`). Lo escribe la operación opcional
  `registrar_pago_externo` del proveedor. Sirve para que el link viejo del cargo, si
  igual se paga, se reconozca como pago doble y se avise.

Columna nullable sin default: sin reescritura ni bloqueo largo. La tabla ya tiene
`tenant_id` + RLS forzada.

Revision ID: pack_subscriptions_0007
Revises: pack_subscriptions_0006
Create Date: 2026-10-09
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "pack_subscriptions_0007"
down_revision = "pack_subscriptions_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pack_subscriptions_charges", sa.Column("pago_externo", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("pack_subscriptions_charges", "pago_externo")
