"""Cita: `created_by` (origen) y `timezone` (zona horaria por cita).

HU-CAL-02 (SPEC-24): el calendario interno guarda quién creó la cita
(`assistant` | `panel` | `customer`) y con qué zona horaria, en vez de asumir
siempre la del negocio. Ambas columnas con default seguro para las filas
existentes.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-04
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pack_calendar_appointments",
        sa.Column("created_by", sa.Text(), nullable=False, server_default="assistant"),
    )
    op.add_column(
        "pack_calendar_appointments",
        sa.Column("timezone", sa.Text(), nullable=False, server_default="America/Bogota"),
    )


def downgrade() -> None:
    op.drop_column("pack_calendar_appointments", "timezone")
    op.drop_column("pack_calendar_appointments", "created_by")
