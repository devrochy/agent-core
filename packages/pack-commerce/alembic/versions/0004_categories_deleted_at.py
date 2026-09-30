"""pack_commerce_categories: soft-delete (deuda registrada, invariante nº 8 de CLAUDE.md)

- `deleted_at timestamptz` — sin borrado físico, se marca en vez de eliminar.
- `created_at timestamptz not null default now()` — faltaba en esta tabla desde su
  creación (0001), todas las demás tablas de negocio del pack ya lo tienen.

No hace falta índice único parcial: no existe ninguna restricción de unicidad sobre
`pack_commerce_categories` hoy (ni `name` ni ninguna otra columna es única), así que
no hay nada que acotar con `WHERE deleted_at IS NULL`.

Revision ID: crm_0004
Revises: crm_0003
Create Date: 2026-09-11
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "crm_0004"
down_revision = "crm_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pack_commerce_categories", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "pack_commerce_categories",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_column("pack_commerce_categories", "created_at")
    op.drop_column("pack_commerce_categories", "deleted_at")
