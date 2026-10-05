"""`updated_at` en clientes y carteras para sync incremental (HU-APP-15, ADR-0097).

Igual que `0015` (promesas): el cursor del sync necesita marcadores de cambio y
estas tablas solo tenían `deleted_at`/`created_at`. Columnas aditivas con
default constante.

Revision ID: pack_cobranza_0016
Revises: pack_cobranza_0015
Create Date: 2026-10-05
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "pack_cobranza_0016"
down_revision = "pack_cobranza_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pack_cobranza_clientes",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "pack_cobranza_carteras",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("pack_cobranza_carteras", "updated_at")
    op.drop_column("pack_cobranza_clientes", "updated_at")
