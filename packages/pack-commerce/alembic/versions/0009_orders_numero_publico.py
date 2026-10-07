"""`public_order_number` deja de ser columna computada (HU-COM-03, D4).

Antes `'OC-' || lpad(order_seq::text, 4, '0')` era COMPUTED y persistida: el
prefijo era fijo para todos los negocios y no había índice único sobre
`(tenant_id, public_order_number)`. Ahora el número lo asigna `crear_pedido`
en código (prefijo configurable por negocio, sin truncar a partir de 10.000)
y se respalda con un índice único por tenant.

Backfill: se conserva el número que ya tenían los pedidos (`OC-NNNN`).

Revision ID: crm_0009
Revises: crm_0008
Create Date: 2026-10-07
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "crm_0009"
down_revision = "crm_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("pack_commerce_orders", "public_order_number")
    op.add_column(
        "pack_commerce_orders",
        sa.Column("public_order_number", sa.Text(), nullable=True),
    )
    op.execute(
        "update pack_commerce_orders "
        "set public_order_number = 'OC-' || lpad(order_seq::text, 4, '0') "
        "where public_order_number is null"
    )
    op.alter_column("pack_commerce_orders", "public_order_number", nullable=False)
    op.create_index(
        "uq_crm_orders_tenant_number",
        "pack_commerce_orders",
        ["tenant_id", "public_order_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_crm_orders_tenant_number", table_name="pack_commerce_orders")
    op.alter_column(
        "pack_commerce_orders",
        "public_order_number",
        type_=sa.Text(),
        existing_nullable=False,
        server_default=sa.text("'OC-' || lpad(order_seq::text, 4, '0')"),
    )
