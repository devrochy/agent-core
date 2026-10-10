"""Referencia única solo entre transacciones vivas (`deleted_at is null`).

`generar_link_pago` regenera el link de una referencia pendiente cuyo link venció:
la transacción vieja se marca `deleted_at` (queda para auditoría y para un webhook
tardío, que busca por `provider_reference`) y se crea otra con la misma referencia.
Con el índice único completo esa segunda fila chocaba; el parcial deja una sola
transacción viva por referencia, que es la invariante que importa.

El downgrade falla si ya hay una referencia con una transacción viva y otra
borrada: volver al índice completo exige decidir antes qué filas sobran.

Revision ID: pack_payments_0003
Revises: pack_payments_0002
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "pack_payments_0003"
down_revision = "pack_payments_0002"
branch_labels = None
depends_on = None

TABLE = "pack_payments_transactions"


def upgrade() -> None:
    op.create_index(
        "uq_pack_payments_tenant_reference_viva",
        TABLE,
        ["tenant_id", "reference"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )
    op.drop_index("uq_pack_payments_tenant_reference", table_name=TABLE)


def downgrade() -> None:
    op.create_index("uq_pack_payments_tenant_reference", TABLE, ["tenant_id", "reference"], unique=True)
    op.drop_index("uq_pack_payments_tenant_reference_viva", table_name=TABLE)
