"""Subsección de las pruebas del catálogo (HU-QA-08, catálogo v15).

La v15 del catálogo agrupa las pruebas por `entorno` → `area` → `subseccion` (tercer nivel con
lista cerrada en solvershub-docs) en lugar del `grupo` libre. La columna queda nula en los
catálogos anteriores a la v15, que se siguen importando y leyendo igual.

**Rollback:** solo el `downgrade`; no hay datos que migrar.

Revision ID: 0101
Revises: 0100
Create Date: 2026-10-21
"""

from __future__ import annotations

from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE qa_items ADD COLUMN IF NOT EXISTS subseccion text")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE qa_items DROP COLUMN IF EXISTS subseccion")
