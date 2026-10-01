"""pack_commerce_product_variants: metadatos de recurrencia del plan (ADR-0083, #153).

Agrega a la variante (que representa un plan vendible cuando tiene `billing_period`):
- `billing_period` — periodicidad del cobro ("mensual"/"anual"); NULL = producto one-shot.
- `trial_days` — días de prueba del plan.
- `entitlements` — límites del plan (clientes, conversaciones, mensajes…).

Revision ID: crm_0006
Revises: crm_0005
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "crm_0006"
down_revision = "crm_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pack_commerce_product_variants",
        sa.Column("billing_period", sa.Text(), nullable=True),
    )
    op.add_column(
        "pack_commerce_product_variants",
        sa.Column("trial_days", sa.Integer(), nullable=True),
    )
    op.add_column(
        "pack_commerce_product_variants",
        sa.Column("entitlements", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("pack_commerce_product_variants", "entitlements")
    op.drop_column("pack_commerce_product_variants", "trial_days")
    op.drop_column("pack_commerce_product_variants", "billing_period")