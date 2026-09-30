"""Link de pago hacia una cuota de cliente (cobro digital, ADR-0067).

Revision ID: pack_cobranza_0011
Revises: pack_cobranza_0010
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "pack_cobranza_0011"
down_revision = "pack_cobranza_0010"
branch_labels = None
depends_on = None

_UUID = UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("pack_cobranza_links_pago", sa.Column("cuota_id", _UUID, nullable=True))
    op.add_column("pack_cobranza_links_pago", sa.Column("prestamo_id", _UUID, nullable=True))
    op.add_column("pack_cobranza_links_pago", sa.Column("cliente_id", _UUID, nullable=True))


def downgrade() -> None:
    op.drop_column("pack_cobranza_links_pago", "cliente_id")
    op.drop_column("pack_cobranza_links_pago", "prestamo_id")
    op.drop_column("pack_cobranza_links_pago", "cuota_id")
