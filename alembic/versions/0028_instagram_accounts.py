"""Instagram: puente conversations.ig_account_id (mismo patrón que
fb_page_id, ADR-0031 -- una MetaConnection de Instagram puede agrupar
varias IgAccount, sobre todo en la ruta Instagram Login), status/
status_reason por cuenta (el cron de refresco puede degradar UNA cuenta
sin afectar a las demás bajo la misma conexión), y subscribed_fields
(paridad con fb_pages/waba_accounts -- la UI debe mostrar los fields
REALES confirmados por Meta, no los pedidos).

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-24
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("ig_account_id", UUID(as_uuid=True), sa.ForeignKey("ig_accounts.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_conversations_ig_account", "conversations", ["ig_account_id"])

    op.add_column("ig_accounts", sa.Column("status", sa.Text(), nullable=False, server_default="connected"))
    op.add_column("ig_accounts", sa.Column("status_reason", sa.Text(), nullable=True))
    op.add_column("ig_accounts", sa.Column("subscribed_fields", ARRAY(sa.Text()), nullable=False, server_default="{}"))
    op.create_check_constraint(
        "ck_ig_accounts_status", "ig_accounts", "status in ('connected','degraded')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_ig_accounts_status", "ig_accounts", type_="check")
    op.drop_column("ig_accounts", "subscribed_fields")
    op.drop_column("ig_accounts", "status_reason")
    op.drop_column("ig_accounts", "status")

    op.execute("drop index if exists ix_conversations_ig_account")
    op.drop_column("conversations", "ig_account_id")
