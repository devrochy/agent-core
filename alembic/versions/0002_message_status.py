"""Agrega status a messages para auditar envíos outbound.

Motivación: si el envío saliente a WhatsApp falla,
antes el mensaje NO se persistía y el fallo quedaba invisible (el asunto se
descartaba silenciosamente). Ahora el mensaje outbound siempre se persiste con
status 'sent' | 'failed' para poder detectar y reenviar envíos rotos.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("status", sa.Text(), nullable=False, server_default="sent"),
    )


def downgrade() -> None:
    op.drop_column("messages", "status")
