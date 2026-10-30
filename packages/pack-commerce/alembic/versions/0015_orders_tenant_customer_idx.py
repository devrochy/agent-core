"""Índice de pedidos por (tenant_id, customer_id) (HU-CRM-20).

«Ver sus pedidos» de la ficha del cliente filtra `GET /pack/commerce/orders` por `customer_id`.
Solo existía `ix_crm_orders_customer (customer_id)` y la invariante pide índices compuestos que
empiecen por `tenant_id`. `CREATE INDEX` toma un bloqueo de escritura sobre la tabla mientras
corre; `pack_commerce_orders` es chica (pedidos confirmados por negocio), así que no se usa
`CONCURRENTLY` (no corre dentro de la transacción de Alembic).

Revision ID: crm_0015
Revises: crm_0014
Create Date: 2026-10-28
"""

from __future__ import annotations

from alembic import op

revision = "crm_0015"
down_revision = "crm_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pack_commerce_orders_tenant_customer "
        "ON pack_commerce_orders (tenant_id, customer_id)"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_pack_commerce_orders_tenant_customer")
