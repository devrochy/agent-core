"""Cola de salida con idempotencia (client_message_id), wamid persistido
(external_message_id), detalle de error de Meta, y máquina de estados de
`statuses` (sent/delivered/read) con orden monótono.

Agrega `conversations.service_window_expires_at` (validación local de la
ventana de 24h antes de encolar un envío libre) y una quinta función
SECURITY DEFINER, `resolve_tenant_for_message`, mismo patrón que
`resolve_tenant_for_message_template` (ADR-0031): el webhook de `statuses`
solo trae el `wamid`, no el tenant -- RLS bloquearía el lookup sin tenant
seteado todavía.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("service_window_expires_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("messages", sa.Column("client_message_id", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("external_message_id", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("error_code", sa.Integer(), nullable=True))
    op.add_column("messages", sa.Column("error_subcode", sa.Integer(), nullable=True))
    op.add_column("messages", sa.Column("error_fbtrace_id", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("error_reason", sa.Text(), nullable=True))
    op.add_column("messages", sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=True))

    op.create_index(
        "uq_messages_tenant_client_message_id", "messages", ["tenant_id", "client_message_id"],
        unique=True, postgresql_where=sa.text("client_message_id is not null"),
    )
    op.create_index(
        "uq_messages_tenant_external_message_id", "messages", ["tenant_id", "external_message_id"],
        unique=True, postgresql_where=sa.text("external_message_id is not null"),
    )

    op.execute("alter table messages drop constraint if exists ck_messages_status")
    op.execute(
        "alter table messages add constraint ck_messages_status "
        "check (status in ('queued','sent','delivered','read','failed'))"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION resolve_tenant_for_message(p_external_message_id text)
        RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT tenant_id FROM messages WHERE external_message_id = p_external_message_id LIMIT 1;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("drop function if exists resolve_tenant_for_message(text)")

    op.execute("alter table messages drop constraint if exists ck_messages_status")

    op.drop_index("uq_messages_tenant_external_message_id", table_name="messages")
    op.drop_index("uq_messages_tenant_client_message_id", table_name="messages")

    op.drop_column("messages", "status_updated_at")
    op.drop_column("messages", "error_reason")
    op.drop_column("messages", "error_fbtrace_id")
    op.drop_column("messages", "error_subcode")
    op.drop_column("messages", "error_code")
    op.drop_column("messages", "external_message_id")
    op.drop_column("messages", "client_message_id")

    op.drop_column("conversations", "service_window_expires_at")
