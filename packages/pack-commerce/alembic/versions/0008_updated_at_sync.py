"""`updated_at` en categorías y promociones para sync incremental (HU-APP-15, ADR-0097).

El cursor del sync de comercio necesita marcadores de cambio; `categories` no
tenía timestamps y `promotions` solo `created_at`. Columnas aditivas con
default constante (sin reescribir).

Revision ID: crm_0008
Revises: crm_0007
Create Date: 2026-10-05
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "crm_0008"
down_revision = "crm_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pack_commerce_categories",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "pack_commerce_promotions",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("pack_commerce_promotions", "updated_at")
    op.drop_column("pack_commerce_categories", "updated_at")
