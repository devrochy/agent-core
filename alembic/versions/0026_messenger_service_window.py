"""Messenger: ventana de servicio por conversación + toggle de Conversation
Routing por Página.

`conversations.service_window_expires_at`: se refresca en cada mensaje
entrante (24h desde el último inbound) -- usado para decidir
messaging_type/tag antes de enviar (core/gateway/meta_messenger.py). No es
exclusivo de Messenger a nivel de esquema (cualquier canal con ventana de
24h podría usarlo), pero hoy solo Messenger lo llena.

`fb_pages.conversation_routing_enabled`: Conversation Routing es opt-in por
Página, activado a mano en Meta Business Suite (no automatizable) -- el
admin marca el toggle en nuestro panel para que el chequeo de thread_owner
se haga antes de cada envío; sin esto, se evita una llamada de Graph API
extra en el caso común (Páginas sin Conversation Routing).

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-23
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("service_window_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_conversations_service_window",
        "conversations",
        ["service_window_expires_at"],
        postgresql_where=sa.text("service_window_expires_at is not null"),
    )
    op.add_column(
        "fb_pages",
        sa.Column("conversation_routing_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("fb_pages", "conversation_routing_enabled")
    op.execute("drop index if exists ix_conversations_service_window")
    op.drop_column("conversations", "service_window_expires_at")
