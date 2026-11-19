"""Índices para la administración de suscripciones (SPEC-35, HU-SUB-08 y HU-SUB-20).

La lista y las métricas del panel filtran por `estado` (suscripciones y cargos) y por
`fecha_vencimiento` (mora). Sin estos índices cada consulta recorre todas las filas del
negocio.

- `ix_pack_subscriptions_subs_tenant_estado`: `(tenant_id, estado)` en las suscripciones.
- `ix_pack_subscriptions_charges_tenant_estado_vence`: `(tenant_id, estado, fecha_vencimiento)`
  en los cargos.

Solo índices: sin cambio de columnas ni de datos. Las tablas son pequeñas por negocio
(`tenant_id` + RLS forzada ya existen), así que el índice normal no bloquea de forma notable.

Revision ID: pack_subscriptions_0008
Revises: pack_subscriptions_0007
Create Date: 2026-10-23
"""
from __future__ import annotations

from alembic import op

revision = "pack_subscriptions_0008"
down_revision = "pack_subscriptions_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_pack_subscriptions_subs_tenant_estado",
        "pack_subscriptions_subscriptions",
        ["tenant_id", "estado"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_pack_subscriptions_charges_tenant_estado_vence",
        "pack_subscriptions_charges",
        ["tenant_id", "estado", "fecha_vencimiento"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_pack_subscriptions_charges_tenant_estado_vence", table_name="pack_subscriptions_charges", if_exists=True)
    op.drop_index("ix_pack_subscriptions_subs_tenant_estado", table_name="pack_subscriptions_subscriptions", if_exists=True)
