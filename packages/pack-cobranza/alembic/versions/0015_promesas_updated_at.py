"""`pack_cobranza_promesas.updated_at` para sync incremental de la app (HU-APP-15, ADR-0097).

El cursor del sync de promesas necesita un marcador de cambio; la tabla solo
tenía `deleted_at`. Columna aditiva con default constante (sin reescribir).

Revision ID: pack_cobranza_0015
Revises: pack_cobranza_0014
Create Date: 2026-10-05
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "pack_cobranza_0015"
down_revision = "pack_cobranza_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pack_cobranza_promesas",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("pack_cobranza_promesas", "updated_at")
