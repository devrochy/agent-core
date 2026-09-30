"""KPIs del día en la liquidación (REQ-APK-003).

Revision ID: pack_cobranza_0010
Revises: pack_cobranza_0009
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "pack_cobranza_0010"
down_revision = "pack_cobranza_0009"
branch_labels = None
depends_on = None

_NUEVAS = (
    ("estimado_acobrar", sa.Numeric(14, 2)),
    ("total_inyeccion", sa.Numeric(14, 2)),
    ("suma_cartera", sa.Numeric(14, 2)),
    ("comision_porcentaje", sa.Numeric(6, 3)),
)


def upgrade() -> None:
    for nombre, tipo in _NUEVAS:
        op.add_column("pack_cobranza_liquidaciones", sa.Column(nombre, tipo, nullable=False, server_default="0"))
    op.add_column("pack_cobranza_liquidaciones", sa.Column("detalle", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("pack_cobranza_liquidaciones", "detalle")
    for nombre, _ in _NUEVAS:
        op.drop_column("pack_cobranza_liquidaciones", nombre)
