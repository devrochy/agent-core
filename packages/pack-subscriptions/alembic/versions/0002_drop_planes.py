"""pack-subscriptions: dropea el catálogo de planes (planes como productos, ADR-0083).

El plan pasó a ser un producto de commerce (variante con `billing_period`). La
tabla `pack_subscriptions_plans` (creada en 0001, solo usada en test) se elimina:
el snapshot del plan lo aporta commerce/admin vía el registry del core.

Revision ID: pack_subscriptions_0002
Revises: pack_subscriptions_0001
Create Date: 2026-10-01
"""

from __future__ import annotations

from alembic import op

revision = "pack_subscriptions_0002"
down_revision = "pack_subscriptions_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on pack_subscriptions_plans")
    op.drop_index("ix_pack_subscriptions_plans_tenant", table_name="pack_subscriptions_plans")
    op.drop_table("pack_subscriptions_plans")


def downgrade() -> None:
    # Greenfield: no se restaura el catálogo de planes (se migra a productos de commerce).
    pass