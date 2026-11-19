"""`public_order_number` deja de ser columna computada (HU-COM-03, D4).

Antes `'OC-' || lpad(order_seq::text, 4, '0')` era COMPUTED y persistida: el
prefijo era fijo para todos los negocios y no había índice único sobre
`(tenant_id, public_order_number)`. Ahora el número lo asigna `crear_pedido`
en código (prefijo configurable por negocio, sin truncar a partir de 10.000)
y se respalda con un índice único por tenant.

Backfill: se conserva el número que ya tenían los pedidos (`OC-NNNN`), salvo desde
el pedido 10.000: `lpad(x, 4)` TRUNCA el texto en Postgres, así que la columna
computada mostraba `OC-8379` para los pedidos 83790..83799 (el mismo bug que corrige
HU-COM-03). Repetir ese cálculo dejaba números duplicados y el índice único abortaba
la migración (visto en la base local el 2026-10-08: seq 837966..837971 → `OC-8379`).
`BACKFILL_NUMERO` rellena con ceros hasta 4 dígitos pero nunca corta. Como
`order_seq` es único por negocio (`uq_crm_orders_tenant_seq`), el número también.
Donde la migración ya corrió sin pedidos ≥ 10.000 (test), el resultado es el mismo.

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

# Número público sin truncar: `OC-0042`, `OC-9999`, `OC-10000`, `OC-837966`.
BACKFILL_NUMERO = "'OC-' || lpad(order_seq::text, greatest(4, length(order_seq::text)), '0')"


def upgrade() -> None:
    op.drop_column("pack_commerce_orders", "public_order_number")
    op.add_column(
        "pack_commerce_orders",
        sa.Column("public_order_number", sa.Text(), nullable=True),
    )
    op.execute(
        "update pack_commerce_orders "
        f"set public_order_number = {BACKFILL_NUMERO} "
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
