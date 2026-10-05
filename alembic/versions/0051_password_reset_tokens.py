"""`password_reset_tokens`: recuperación de contraseña self-service (HU-APP-02, ADR-0094).

Tabla de SISTEMA (sin RLS): infraestructura de identidad como `admin_sessions`,
se consulta fuera del contexto de un negocio (el admin pide recuperar su
contraseña sin haber elegido tenant). Sin datos de negocio; grants mínimos a
app_runtime (ADR-0094).

Revision ID: 0051
Revises: 0050
Create Date: 2026-10-05
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

_TABLE = "password_reset_tokens"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "admin_id",
            UUID(as_uuid=True),
            sa.ForeignKey("admins.id"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(f"ix_{_TABLE}_admin", _TABLE, ["admin_id"])
    op.create_index(f"ix_{_TABLE}_token_hash", _TABLE, ["token_hash"])
    op.execute(f"grant select, insert, update, delete on {_TABLE} to app_runtime")


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_token_hash", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_admin", table_name=_TABLE)
    op.drop_table(_TABLE)
