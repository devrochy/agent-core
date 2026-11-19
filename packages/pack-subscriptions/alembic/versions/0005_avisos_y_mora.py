"""Avisos de cobro y pausa por mora (HU-SUB-05).

- `pack_subscriptions_charges.link_url`: el link de pago del cargo; los
  recordatorios lo reusan en vez de crear uno nuevo cada vez.
- `pack_subscriptions_subscriptions.motivo_pausa`: `mora` cuando la pausa la hizo
  el cron; pagar el cargo reactiva solo esas (una pausa del cliente no).

Columnas nullables: sin reescritura de tabla.

Revision ID: pack_subscriptions_0005
Revises: pack_subscriptions_0004
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "pack_subscriptions_0005"
down_revision = "pack_subscriptions_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pack_subscriptions_charges", sa.Column("link_url", sa.Text(), nullable=True))
    op.add_column("pack_subscriptions_subscriptions", sa.Column("motivo_pausa", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("pack_subscriptions_subscriptions", "motivo_pausa")
    op.drop_column("pack_subscriptions_charges", "link_url")
