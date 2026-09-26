"""IMEI único por tenant (autovinculación, HU-39/43).

Revision ID: pack_cobranza_0012
Revises: pack_cobranza_0011
Create Date: 2026-09-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "pack_cobranza_0012"
down_revision = "pack_cobranza_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_pack_cobranza_device_imei",
        "pack_cobranza_devices",
        ["tenant_id", "imei"],
        unique=True,
        postgresql_where=sa.text("imei is not null and deleted_at is null"),
    )


def downgrade() -> None:
    op.drop_index("uq_pack_cobranza_device_imei", table_name="pack_cobranza_devices")
