"""Schema inicial del core.

Incluye:
- Extensión pgvector.
- Todas las tablas base del core.
- Trigger de inmutabilidad sobre audit_log (no UPDATE, no DELETE).
- Índices de acceso frecuente.

Revision ID: 0001
Revises:
Create Date: 2026-08-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "app_settings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("assistant_name", sa.Text(), nullable=False, server_default="Asistente"),
        sa.Column("business_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("language", sa.Text(), nullable=False, server_default="es"),
        sa.Column("bot_paused", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("llm_provider", sa.Text(), nullable=True),
        sa.Column("llm_model", sa.Text(), nullable=True),
        sa.Column("llm_api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("behavior_config", JSONB(), nullable=False, server_default="{}"),
        sa.Column("brand_voice", sa.Text(), nullable=False, server_default=""),
        sa.Column("escalation_config", JSONB(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "customers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("contact_phone", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("bot_paused", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("attributes", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("channel", "external_id", name="uq_customer_channel_external"),
    )
    op.create_index("ix_customers_contact_phone", "customers", ["contact_phone"])

    op.create_table(
        "channel_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("credentials_encrypted", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("provider", "external_id", name="uq_connection_provider_external"),
    )

    op.create_table(
        "conversations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("state", JSONB(), nullable=False, server_default="{}"),
        sa.Column("channel", sa.Text(), nullable=False, server_default="whatsapp"),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("channel_connections.id"), nullable=True),
        sa.Column("bot_paused", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_conversations_customer", "conversations", ["customer_id"])
    op.create_index("ix_conversations_status", "conversations", ["status"])

    op.create_table(
        "messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("sender_type", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_calls", JSONB(), nullable=True),
        sa.Column("media_type", sa.Text(), nullable=True),
        sa.Column("media_url", sa.Text(), nullable=True),
        sa.Column("media_transcription", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])

    op.create_table(
        "plugins",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False, server_default="0.0.0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="detected"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "plugin_configs",
        sa.Column("plugin_id", sa.Text(), sa.ForeignKey("plugins.id"), primary_key=True),
        sa.Column("config_encrypted", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "admins",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("username", sa.Text(), nullable=True, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="master"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "admin_sessions",
        sa.Column("token", sa.Text(), primary_key=True),
        sa.Column("admin_id", UUID(as_uuid=True), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "escalations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("assigned_admin_id", UUID(as_uuid=True), sa.ForeignKey("admins.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_escalations_status", "escalations", ["status"])

    op.create_table(
        "llm_usage",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_llm_usage_created", "llm_usage", ["created_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("input", JSONB(), nullable=True),
        sa.Column("output", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    # Inmutabilidad: el audit log solo acepta INSERT
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_audit_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log es inmutable (solo INSERT)';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER audit_log_immutable
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION prevent_audit_mutation();
        """
    )

    op.create_table(
        "memory_vectors",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", UUID(as_uuid=True), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=True),
        sa.Column("meta", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_memory_vectors_customer", "memory_vectors", ["customer_id"])


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_immutable ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS prevent_audit_mutation")
    for table in [
        "memory_vectors", "audit_log", "llm_usage", "escalations", "admin_sessions",
        "admins", "plugin_configs", "plugins", "messages", "conversations",
        "channel_connections", "customers", "app_settings",
    ]:
        op.drop_table(table)
