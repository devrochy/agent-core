"""Multi-tenant para el esquema crm_* (deuda fechada Sprint 3, ADR-0001).

- tenant_id + RLS + policy + índice compuesto en las 13 tablas crm_*.
- UNIQUEs globales → compuestos por tenant (idempotency de Lead/Order/Batch,
  sku de variantes, order_seq).
- Numeración de pedidos por tenant (ADR-0018): crm_order_counters +
  crm_next_order_seq(tenant).
- GRANT explícito a app_runtime (el rol de runtime NO es dueño de estas tablas).

Revision ID: crm_0002
Revises: crm_0001
Create Date: 2026-08-26
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "crm_0002"
down_revision = "crm_0001"
branch_labels = None
depends_on = None

TABLES = [
    "crm_leads",
    "crm_categories",
    "crm_category_complements",
    "crm_products",
    "crm_product_variants",
    "crm_inventory",
    "crm_promotions",
    "crm_promotion_redemptions",
    "crm_quotes",
    "crm_quote_items",
    "crm_orders",
    "crm_order_items",
    "crm_order_item_batches",
]

TENANT1 = "00000000-0000-0000-0000-000000000001"
RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    # 1. tenant_id (nullable -> backfill -> NOT NULL)
    for table in TABLES:
        op.add_column(table, sa.Column("tenant_id", UUID(as_uuid=True), nullable=True))
    for table in TABLES:
        op.execute(f"update {table} set tenant_id = '{TENANT1}' where tenant_id is null")
    for table in TABLES:
        op.alter_column(table, "tenant_id", nullable=False)

    # 2. RLS + policy de aislamiento
    for table in TABLES:
        op.execute(f"alter table {table} enable row level security")
        op.execute(
            f"create policy tenant_isolation on {table} "
            f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
        )

    # 3. UNIQUEs globales -> compuestos por tenant
    op.execute("alter table crm_leads drop constraint if exists crm_leads_idempotency_key_key")
    op.execute(
        "create unique index if not exists uq_crm_leads_tenant_idem "
        "on crm_leads (tenant_id, idempotency_key) where idempotency_key is not null"
    )
    op.execute("alter table crm_product_variants drop constraint if exists crm_product_variants_sku_key")
    op.execute(
        "create unique index if not exists uq_crm_variants_tenant_sku "
        "on crm_product_variants (tenant_id, sku)"
    )
    op.execute("alter table crm_orders drop constraint if exists crm_orders_idempotency_key_key")
    op.execute(
        "create unique index if not exists uq_crm_orders_tenant_idem "
        "on crm_orders (tenant_id, idempotency_key)"
    )
    op.execute("alter table crm_orders drop constraint if exists crm_orders_order_seq_key")
    op.execute(
        "create unique index if not exists uq_crm_orders_tenant_seq "
        "on crm_orders (tenant_id, order_seq)"
    )
    op.execute(
        "alter table crm_order_item_batches drop constraint if exists "
        "crm_order_item_batches_idempotency_key_key"
    )
    op.execute(
        "create unique index if not exists uq_crm_batches_tenant_idem "
        "on crm_order_item_batches (tenant_id, idempotency_key)"
    )

    # 4. Numeración de pedido por tenant (ADR-0018)
    op.execute(
        f"""
        create table if not exists crm_order_counters (
            tenant_id uuid primary key,
            last_seq integer not null default 0
        );
        alter table crm_order_counters enable row level security;
        create policy tenant_isolation on crm_order_counters
          using (tenant_id = {RLS}) with check (tenant_id = {RLS});
        create or replace function crm_next_order_seq(p_tenant uuid) returns integer
        language sql as $$
            insert into crm_order_counters (tenant_id, last_seq)
            values (p_tenant, 1)
            on conflict (tenant_id) do update set last_seq = crm_order_counters.last_seq + 1
            returning last_seq;
        $$;
        """
    )

    # 5. Índices compuestos por tenant
    for table in TABLES:
        op.create_index(f"ix_{table}_tenant", table, ["tenant_id"])

    # 6. GRANT al rol de runtime (no es dueño de estas tablas; RLS debe aplicar). Solo sobre las
    # tablas de este pack: antes era `on all tables in schema public` y, en una base nueva (los packs
    # migran después del head del core), deshacía los REVOKE del core (p. ej. 0066 y 0068-0069 de
    # HU-AOS-02). En las bases donde ya corrió, este cambio no tiene efecto.
    for table in [*TABLES, "crm_order_counters"]:
        op.execute(f"grant select, insert, update, delete on {table} to app_runtime")
    op.execute("grant usage, select on all sequences in schema public to app_runtime")


def downgrade() -> None:
    # 1. RLS
    for table in TABLES:
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.execute(f"alter table {table} disable row level security")

    # 2. Numeración por tenant
    op.execute("drop policy if exists tenant_isolation on crm_order_counters")
    op.execute("alter table crm_order_counters disable row level security")
    op.execute("drop function if exists crm_next_order_seq(uuid)")
    op.drop_table("crm_order_counters")

    # 3. UNIQUEs compuestos -> globales
    op.execute("drop index if exists uq_crm_leads_tenant_idem")
    op.execute("alter table crm_leads add constraint crm_leads_idempotency_key_key unique (idempotency_key)")
    op.execute("drop index if exists uq_crm_variants_tenant_sku")
    op.execute("alter table crm_product_variants add constraint crm_product_variants_sku_key unique (sku)")
    op.execute("drop index if exists uq_crm_orders_tenant_idem")
    op.execute("alter table crm_orders add constraint crm_orders_idempotency_key_key unique (idempotency_key)")
    op.execute("drop index if exists uq_crm_orders_tenant_seq")
    op.execute("alter table crm_orders add constraint crm_orders_order_seq_key unique (order_seq)")
    op.execute("drop index if exists uq_crm_batches_tenant_idem")
    op.execute(
        "alter table crm_order_item_batches add constraint "
        "crm_order_item_batches_idempotency_key_key unique (idempotency_key)"
    )

    # 4. Índices y columna
    for table in TABLES:
        op.execute(f"drop index if exists ix_{table}_tenant")
    for table in TABLES:
        op.drop_column(table, "tenant_id")