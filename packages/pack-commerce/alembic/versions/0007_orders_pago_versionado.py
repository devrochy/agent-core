"""pack_commerce_orders: referencia y link de pago versionados (HU-COM-01).

El link de pago de un pedido se rige por el total del servidor. Se guardan:
- `payment_reference`: referencia versionada (`OC-0001`, `OC-0001-r2`…) que se
  regenera al cambiar el total; es la que ve la pasarela.
- `payment_link_url`: el link vigente, para exponerlo/expirar el anterior.

`total` ya existe en la tabla.

Revision ID: crm_0007
Revises: crm_0006
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "crm_0007"
down_revision = "crm_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pack_commerce_orders",
        sa.Column("payment_reference", sa.Text(), nullable=True),
    )
    op.add_column(
        "pack_commerce_orders",
        sa.Column("payment_link_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "pack_commerce_orders",
        sa.Column("payment_link_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("pack_commerce_orders", "payment_link_version")
    op.drop_column("pack_commerce_orders", "payment_link_url")
    op.drop_column("pack_commerce_orders", "payment_reference")
