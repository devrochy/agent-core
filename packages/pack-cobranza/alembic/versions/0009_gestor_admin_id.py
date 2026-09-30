"""Gestor = colaborador del core: `admin_id` y sin credenciales locales (ADR-0066).

Revision ID: pack_cobranza_0009
Revises: pack_cobranza_0008
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "pack_cobranza_0009"
down_revision = "pack_cobranza_0008"
branch_labels = None
depends_on = None

_UUID = UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("pack_cobranza_gestores", sa.Column("admin_id", _UUID, nullable=True))
    op.create_index(
        "uq_pack_cobranza_gestor_admin",
        "pack_cobranza_gestores",
        ["tenant_id", "admin_id"],
        unique=True,
        postgresql_where=sa.text("admin_id is not null"),
    )
    op.drop_column("pack_cobranza_gestores", "password_hash")
    op.drop_column("pack_cobranza_gestores", "usuario")


def downgrade() -> None:
    op.add_column("pack_cobranza_gestores", sa.Column("usuario", sa.Text(), nullable=True))
    op.add_column("pack_cobranza_gestores", sa.Column("password_hash", sa.Text(), nullable=True))
    op.drop_index("uq_pack_cobranza_gestor_admin", table_name="pack_cobranza_gestores")
    op.drop_column("pack_cobranza_gestores", "admin_id")
