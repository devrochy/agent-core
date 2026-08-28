"""Extracción a pack-commerce: rename en sitio de las tablas crm_* (ADR-0020).

- 13 tablas de negocio + crm_order_counters → pack_commerce_*.
- Secuencia crm_orders_seq → pack_commerce_orders_seq.
- Función crm_next_order_seq → pack_commerce_next_order_seq (apunta al contador nuevo).
- Columna `custom jsonb` (ADR-0004) en las 13 tablas de negocio.

ALTER TABLE ... RENAME es metadata-only en Postgres: instantáneo y transaccional.
Las policies RLS e índices siguen al rename automáticamente.

Revision ID: crm_0003
Revises: crm_0002
Create Date: 2026-08-26
"""
from __future__ import annotations

from alembic import op

revision = "crm_0003"
down_revision = "crm_0002"
branch_labels = None
depends_on = None

BUSINESS_RENAMES = [
    ("crm_leads", "pack_commerce_leads"),
    ("crm_categories", "pack_commerce_categories"),
    ("crm_category_complements", "pack_commerce_category_complements"),
    ("crm_products", "pack_commerce_products"),
    ("crm_product_variants", "pack_commerce_product_variants"),
    ("crm_inventory", "pack_commerce_inventory"),
    ("crm_promotions", "pack_commerce_promotions"),
    ("crm_promotion_redemptions", "pack_commerce_promotion_redemptions"),
    ("crm_quotes", "pack_commerce_quotes"),
    ("crm_quote_items", "pack_commerce_quote_items"),
    ("crm_orders", "pack_commerce_orders"),
    ("crm_order_items", "pack_commerce_order_items"),
    ("crm_order_item_batches", "pack_commerce_order_item_batches"),
]


def upgrade() -> None:
    # 1. rename de tablas de negocio
    for old, new in BUSINESS_RENAMES:
        op.execute(f"alter table if exists {old} rename to {new}")
        op.execute(
            f"alter table {new} add column if not exists custom jsonb not null default '{{}}'"
        )

    # 2. contador de pedidos por tenant
    op.execute("alter table if exists crm_order_counters rename to pack_commerce_order_counters")

    # 3. secuencia y función de numeración
    op.execute("alter sequence if exists crm_orders_seq rename to pack_commerce_orders_seq")
    op.execute("drop function if exists crm_next_order_seq(uuid)")
    op.execute(
        """
        create or replace function pack_commerce_next_order_seq(p_tenant uuid) returns integer
        language sql as $$
            insert into pack_commerce_order_counters (tenant_id, last_seq)
            values (p_tenant, 1)
            on conflict (tenant_id) do update set last_seq = pack_commerce_order_counters.last_seq + 1
            returning last_seq;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("drop function if exists pack_commerce_next_order_seq(uuid)")
    op.execute("alter sequence if exists pack_commerce_orders_seq rename to crm_orders_seq")
    op.execute("alter table if exists pack_commerce_order_counters rename to crm_order_counters")

    for old, new in reversed(BUSINESS_RENAMES):
        op.execute(f"alter table if exists {new} rename to {old}")
        op.execute(f"alter table {old} drop column if exists custom")