"""Borrado lógico de productos (HU-QA-10).

`pack_commerce_products` era la única tabla del catálogo sin `deleted_at`: retirar un lote de
datos de prueba desactivaba variantes y borraba categorías, pero los productos seguían en el
listado del panel. Se agrega `deleted_at` para que el retiro sea completo y las lecturas puedan
ocultarlos.

Revision ID: crm_0014
Revises: crm_0013
Create Date: 2026-10-28
"""

from __future__ import annotations

from alembic import op

revision = "crm_0014"
down_revision = "crm_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE pack_commerce_products ADD COLUMN IF NOT EXISTS deleted_at timestamptz")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_pack_commerce_products_tenant_vivos "
        "ON pack_commerce_products (tenant_id) WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_pack_commerce_products_tenant_vivos")
    op.execute("ALTER TABLE pack_commerce_products DROP COLUMN IF EXISTS deleted_at")
