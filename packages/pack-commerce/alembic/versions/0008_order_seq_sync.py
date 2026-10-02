"""pack_commerce_next_order_seq: arrancar por encima del máximo existente.

La función insertaba `1` en el contador del tenant cuando no existía, pero si la
tabla `pack_commerce_orders` ya tenía pedidos (tenant real recién adoptado, o
tests que insertan directo), el contador colisionaba con `uq_crm_orders_tenant_seq`.
Esta migración hace que el contador arranque en `max(order_seq) + 1` la primera
vez, y siga incrementando después. Cierra el defecto de numeración (HU-COM-03).

Revision ID: crm_0008
Revises: crm_0007
Create Date: 2026-10-02
"""

from __future__ import annotations

from alembic import op

revision = "crm_0008"
down_revision = "crm_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create or replace function pack_commerce_next_order_seq(p_tenant uuid) returns integer
        language sql as $$
            insert into pack_commerce_order_counters (tenant_id, last_seq)
            values (
                p_tenant,
                coalesce(
                    (select max(order_seq) from pack_commerce_orders where tenant_id = p_tenant),
                    0
                ) + 1
            )
            on conflict (tenant_id) do update
                set last_seq = greatest(
                    pack_commerce_order_counters.last_seq,
                    coalesce(
                        (select max(order_seq) from pack_commerce_orders where tenant_id = p_tenant),
                        0
                    )
                ) + 1
            returning last_seq;
        $$;
        """
    )


def downgrade() -> None:
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
