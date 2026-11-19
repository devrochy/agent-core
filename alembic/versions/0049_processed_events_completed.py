"""Claim de idempotencia con estado: `completed_at` (HU-ARCH-06, ADR-0090).

Hasta ahora `processed_events` no distinguía "en vuelo" de "terminado": si el
proceso moría a mitad de un turno, el claim quedaba insertado y el reclamo
posterior lo veía como duplicado (mensaje perdido en silencio). Se agrega
`completed_at` (NULL = en vuelo o abandonado; con fecha = procesado).

Las filas existentes son eventos ya procesados: se completan con `handled_at`
(su fecha de claim) para no revivir históricos.

Revision ID: 0049
Revises: 0048
Create Date: 2026-10-04
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "processed_events",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Los claims históricos representan eventos ya procesados.
    op.execute("update processed_events set completed_at = handled_at where completed_at is null")


def downgrade() -> None:
    op.drop_column("processed_events", "completed_at")
