"""app_settings.web_chat_config: chat web embebible (HU-CHAN-02).

JSONB con {"enabled", "key", "allowed_origins", "turnstile_secret"}. La clave
pública va en el fragmento del widget; el secreto de Turnstile nunca se expone.

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-03
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column("web_chat_config", sa.dialects.postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "web_chat_config")
