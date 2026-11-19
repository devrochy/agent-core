"""Datos de alta del servicio en los pedidos de planes (HU-SH-03, ADR-0121).

Un pedido de un plan guarda lo que el checkout pidió para dar de alta el servicio y que
hoy se validaba pero se perdía: correo y teléfono del comprador y, si el negocio los pide
en `checkout_fields.plan`, el nombre de su negocio y su tipo de negocio (`vertical`, un id
de preset validado contra la lista, no texto libre). El aprovisionamiento automático lo
lee por la referencia del pedido.

Columna JSONB nullable sin default: los pedidos que no son de planes quedan en NULL, sin
reescritura de la tabla. La tabla ya tiene `tenant_id` + RLS forzada (crm_0005).

Revision ID: crm_0011
Revises: crm_0010
Create Date: 2026-10-09
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "crm_0011"
down_revision = "crm_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pack_commerce_orders", sa.Column("datos_alta", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("pack_commerce_orders", "datos_alta")
