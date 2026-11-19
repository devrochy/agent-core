"""`app_settings.currency`: moneda del negocio (HU-UI-01).

Fuente única de la moneda visible al negocio (hoy los montos de comercio/cobranza
son COP). Los costos de proveedor siguen en USD (`usage_ledger.cost_usd`); la
conversión por TRM vive en el billing del core (HU-BILL-03), no en la UI.

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-03
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column("currency", sa.Text(), nullable=False, server_default="COP"),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "currency")
