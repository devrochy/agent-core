"""Estado de la activación del plan en el pedido (HU-SUB-09).

Commerce ya no sabe qué pack cobra los planes: el core resuelve el proveedor del
negocio. Si no hay uno claro (dos habilitados sin elección, o ninguno), el pedido
queda pagado y la activación `pendiente` con el motivo, visible en el panel; un
cron la reintenta cuando se corrige la configuración.

Columnas nullable sin default: pedidos sin planes quedan en NULL. La tabla ya
tiene `tenant_id` + RLS forzada; no hace falta índice nuevo (el cron filtra por
tenant y el volumen de pendientes es mínimo).

Revision ID: crm_0010
Revises: crm_0009
Create Date: 2026-10-08
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "crm_0010"
down_revision = "crm_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pack_commerce_orders", sa.Column("plan_activation_status", sa.Text(), nullable=True))
    op.add_column("pack_commerce_orders", sa.Column("plan_activation_error", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_pack_commerce_orders_plan_activation_status",
        "pack_commerce_orders",
        "plan_activation_status is null or plan_activation_status in ('pendiente', 'activada')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_pack_commerce_orders_plan_activation_status", "pack_commerce_orders", type_="check")
    op.drop_column("pack_commerce_orders", "plan_activation_error")
    op.drop_column("pack_commerce_orders", "plan_activation_status")
