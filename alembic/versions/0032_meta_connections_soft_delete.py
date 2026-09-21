"""meta_connections: soft-delete (deleted_at) -- invariante nº8 de CLAUDE.md.

Hasta ahora `MetaConnection` no tenía forma de "eliminar" una conexión
(Embedded Signup o BYOK) desde el panel -- solo `status` (activar/
desactivar vía "connected"/"revoked", ya existía en el CHECK). Se agrega
`deleted_at` con el mismo patrón que `ChannelConnection`: el índice único
`(tenant_id, channel, environment, meta_app_id)` pasa a ser PARCIAL
(`where deleted_at is null`) para poder reconectar la misma app después de
"eliminar" una conexión vieja sin chocar con el unique.

Revision ID: 0032
Revises: 0031
Create Date: 2026-09-21
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meta_connections", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("uq_meta_connections_tenant_app", "meta_connections", type_="unique")
    op.create_index(
        "uq_meta_connections_tenant_app",
        "meta_connections",
        ["tenant_id", "channel", "environment", "meta_app_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )


def downgrade() -> None:
    op.drop_index("uq_meta_connections_tenant_app", table_name="meta_connections")
    op.create_unique_constraint(
        "uq_meta_connections_tenant_app", "meta_connections", ["tenant_id", "channel", "environment", "meta_app_id"]
    )
    op.drop_column("meta_connections", "deleted_at")
