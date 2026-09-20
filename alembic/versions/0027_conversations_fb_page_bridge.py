"""conversations.fb_page_id: qué Página de Messenger específica corresponde
a esta conversación.

Hallazgo auditor-multitenant (2026-09-23, severidad media): una
`MetaConnection` de Messenger puede agrupar VARIAS `FbPage` (se conectan
todas las que el admin puede manejar en un solo flujo de Facebook Login).
Sin esta columna, `sender.py::_resolve_messenger_credentials` no tenía
forma de saber CUÁL Página corresponde a una conversación puntual y tomaba
la primera -- un tenant con más de una Página podía terminar respondiendo
"como" la Página equivocada. No es una fuga entre tenants (ambas Páginas
son del mismo tenant), pero sí un bug de enrutamiento real de cara al
cliente final.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-23
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("fb_page_id", UUID(as_uuid=True), sa.ForeignKey("fb_pages.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_conversations_fb_page", "conversations", ["fb_page_id"])


def downgrade() -> None:
    op.execute("drop index if exists ix_conversations_fb_page")
    op.drop_column("conversations", "fb_page_id")
