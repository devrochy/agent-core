"""`app_settings.timezone`: zona horaria del negocio (HU-CAL-04, ADR-0091).

Fuente de la fecha y hora actual que llega al prompt de cada turno y del
horario de atención cuando no trae zona propia. Columna con default constante:
Postgres llena las filas existentes sin reescribir la tabla. No crea tabla, así
que RLS no cambia.

Revision ID: 0050
Revises: 0049
Create Date: 2026-10-04
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column("timezone", sa.Text(), nullable=False, server_default="America/Bogota"),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "timezone")
