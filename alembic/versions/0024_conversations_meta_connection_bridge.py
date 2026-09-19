"""conversations.meta_connection_id: puente para que el envío saliente
(core/gateway/sender.py) pueda resolver credenciales de conexiones creadas
por el wizard de Embedded Signup (`MetaConnection` + `meta_token_store`),
sin migrar el FK existente `conversations.connection_id -> channel_
connections.id` (columna vieja, sigue funcionando igual para conexiones
manuales). Columna PARALELA, nullable, poblada por
`core/orchestrator/consumer.py` además del lookup viejo -- ver
docs/adr/0030-connection-wizards-maquina-estados.md.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-22
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "meta_connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("meta_connections.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_conversations_meta_connection", "conversations", ["meta_connection_id"])


def downgrade() -> None:
    op.execute("drop index if exists ix_conversations_meta_connection")
    op.drop_column("conversations", "meta_connection_id")
