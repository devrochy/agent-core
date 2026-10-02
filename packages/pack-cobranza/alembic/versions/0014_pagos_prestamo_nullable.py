"""`pack_cobranza_pagos.prestamo_id` nullable (HU-SUB-02).

El pago en línea de una cuota de SUSCRIPCIÓN (ADR-0082) insertaba
`Pago(prestamo_id=None)` contra una columna NOT NULL: el bus tragaba el error,
la cuota quedaba sin pagar y el link sin usar. `0013` hizo nullable
`pack_cobranza_cuotas.prestamo_id` pero no el de `pagos`.

Revision ID: pack_cobranza_0014
Revises: pack_cobranza_0013
Create Date: 2026-10-02
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "pack_cobranza_0014"
down_revision = "pack_cobranza_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "pack_cobranza_pagos",
        "prestamo_id",
        existing_type=UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "pack_cobranza_pagos",
        "prestamo_id",
        existing_type=UUID(as_uuid=True),
        nullable=False,
    )
