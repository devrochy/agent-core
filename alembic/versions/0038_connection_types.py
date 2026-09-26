"""Catálogo de tipos de conexión y generalización de `channel_connections` (ADR-0069).

- `connection_types`: tabla de SISTEMA (sin `tenant_id`, sin RLS), excepción
  deliberada a la invariante #1 con el mismo criterio que `plans` (ADR-0045) y
  `webhook_events` (ADR-0028): el catálogo es global y compartido. Su fuente de
  verdad es `core/gateway/connection_types.py`; esta migración la siembra y
  `sync_connection_types()` la mantiene al día. `app_runtime` solo puede LEER:
  se escribe vía `upsert_connection_type()` SECURITY DEFINER.
- `channel_connections` gana `type_id` y `category` (desnormalizada para el índice;
  sin CHECK, la valida el catálogo). Backfill de las filas existentes a
  `whatsapp_meta` / `comunicacion`.

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-25
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

# Debe coincidir con `core/gateway/connection_types.py`.
_META_SCHEMA = {
    "type": "object",
    "properties": {
        "phone_number_id": {"type": "string", "title": "Phone Number ID"},
        "app_secret": {"type": "string", "title": "App Secret", "format": "password", "writeOnly": True},
        "access_token": {"type": "string", "title": "Access Token", "format": "password", "writeOnly": True},
        "verify_token": {"type": "string", "title": "Verify Token", "format": "password", "writeOnly": True},
    },
}
_EMAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "host": {"type": "string", "title": "Servidor SMTP", "description": "ej. smtp.gmail.com"},
        "port": {"type": "integer", "title": "Puerto", "default": 587, "minimum": 1, "maximum": 65535},
        "username": {"type": "string", "title": "Usuario SMTP"},
        "password": {"type": "string", "title": "Contraseña SMTP", "format": "password", "writeOnly": True},
        "from_address": {"type": "string", "title": "Remitente (email)"},
        "use_tls": {"type": "boolean", "title": "Usar TLS", "default": True},
    },
}
_CALENDAR_SCHEMA = {
    "type": "object",
    "properties": {
        "access_token": {"type": "string", "format": "password", "writeOnly": True},
        "refresh_token": {"type": "string", "format": "password", "writeOnly": True},
        "expires_at": {"type": "integer", "title": "Expiración (epoch)"},
        "account_email": {"type": "string", "title": "Cuenta de Google"},
    },
}

_TYPES: list[dict] = [
    {
        "id": "whatsapp_meta",
        "category": "comunicacion",
        "channel": "whatsapp",
        "label": "WhatsApp",
        "provider": "meta",
        "icon": "bi-whatsapp",
        "description": "Meta Cloud API",
        "credentials_schema": _META_SCHEMA,
        "external_id_key": "phone_number_id",
        "produces_conversations": True,
        "available": True,
        "setup_kind": "wizard",
        "sort_order": 10,
    },
    {
        "id": "instagram_meta",
        "category": "comunicacion",
        "channel": "instagram",
        "label": "Instagram",
        "provider": "meta",
        "icon": "bi-instagram",
        "description": "Mensajes directos vía Meta",
        "credentials_schema": {},
        "external_id_key": "account_id",
        "produces_conversations": True,
        "available": False,
        "setup_kind": "wizard",
        "sort_order": 20,
    },
    {
        "id": "messenger_meta",
        "category": "comunicacion",
        "channel": "messenger",
        "label": "Messenger",
        "provider": "meta",
        "icon": "bi-messenger",
        "description": "Página de Facebook vía Meta",
        "credentials_schema": {},
        "external_id_key": "page_id",
        "produces_conversations": True,
        "available": False,
        "setup_kind": "wizard",
        "sort_order": 30,
    },
    {
        "id": "email_smtp",
        "category": "correo",
        "channel": "email",
        "label": "Correo (SMTP)",
        "provider": "smtp",
        "icon": "bi-envelope",
        "description": "Envío de correos desde tu propio servidor SMTP",
        "credentials_schema": _EMAIL_SCHEMA,
        "external_id_key": "from_address",
        "produces_conversations": True,
        "available": True,
        "setup_kind": "manual",
        "sort_order": 40,
    },
    {
        "id": "google_calendar",
        "category": "calendario",
        "channel": "",
        "label": "Google Calendar",
        "provider": "google",
        "icon": "bi-calendar-check",
        "description": "Agendamiento de citas con OAuth2 de Google",
        "credentials_schema": _CALENDAR_SCHEMA,
        "external_id_key": "account_email",
        "produces_conversations": False,
        "available": True,
        "setup_kind": "wizard",
        "sort_order": 50,
    },
]

_FUNCTION_SIGNATURE = (
    "upsert_connection_type(text, text, text, text, text, text, text, jsonb, text, "
    "boolean, boolean, text, int)"
)


def _seed_types() -> None:
    for t in _TYPES:
        op.execute(
            sa.text(
                "select upsert_connection_type("
                ":id, :category, :channel, :label, :provider, :icon, :description, "
                "cast(:credentials_schema as jsonb), :external_id_key, "
                ":produces_conversations, :available, :setup_kind, :sort_order)"
            ).bindparams(
                id=t["id"],
                category=t["category"],
                channel=t["channel"],
                label=t["label"],
                provider=t["provider"],
                icon=t["icon"],
                description=t["description"],
                credentials_schema=json.dumps(t["credentials_schema"]),
                external_id_key=t["external_id_key"],
                produces_conversations=t["produces_conversations"],
                available=t["available"],
                setup_kind=t["setup_kind"],
                sort_order=t["sort_order"],
            )
        )


def upgrade() -> None:
    op.create_table(
        "connection_types",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False, server_default=""),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False, server_default=""),
        sa.Column("icon", sa.Text(), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("credentials_schema", JSONB(), nullable=False, server_default="{}"),
        sa.Column("external_id_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("produces_conversations", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("available", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("setup_kind", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    # `connection_types` es de sistema: sin RLS. `app_runtime` SOLO lee; se
    # escribe vía `upsert_connection_type()` SECURITY DEFINER (dueño), mismo
    # criterio que `plans`/`upsert_plan` (ADR-0045).
    op.execute("revoke insert, update, delete on connection_types from app_runtime")
    op.execute("grant select on connection_types to app_runtime")

    op.execute(
        """
        create or replace function upsert_connection_type(
            p_id text, p_category text, p_channel text, p_label text, p_provider text,
            p_icon text, p_description text, p_credentials_schema jsonb,
            p_external_id_key text, p_produces_conversations boolean, p_available boolean,
            p_setup_kind text, p_sort_order int
        ) returns void
        language sql
        security definer
        set search_path = public
        as $$
            insert into connection_types
                (id, category, channel, label, provider, icon, description,
                 credentials_schema, external_id_key, produces_conversations,
                 available, setup_kind, sort_order)
            values
                (p_id, p_category, p_channel, p_label, p_provider, p_icon, p_description,
                 p_credentials_schema, p_external_id_key, p_produces_conversations,
                 p_available, p_setup_kind, p_sort_order)
            on conflict (id) do update set
                category = excluded.category,
                channel = excluded.channel,
                label = excluded.label,
                provider = excluded.provider,
                icon = excluded.icon,
                description = excluded.description,
                credentials_schema = excluded.credentials_schema,
                external_id_key = excluded.external_id_key,
                produces_conversations = excluded.produces_conversations,
                available = excluded.available,
                setup_kind = excluded.setup_kind,
                sort_order = excluded.sort_order,
                updated_at = now();
        $$
        """
    )
    op.execute(f"grant execute on function {_FUNCTION_SIGNATURE} to app_runtime")

    # Generalización de channel_connections: tipo + categoría desnormalizada.
    op.add_column("channel_connections", sa.Column("type_id", sa.Text(), nullable=True))
    op.add_column("channel_connections", sa.Column("category", sa.Text(), nullable=True))
    op.execute("update channel_connections set type_id = 'whatsapp_meta' where type_id is null")
    op.execute("update channel_connections set category = 'comunicacion' where category is null")
    op.create_index(
        "ix_connections_tenant_category_type",
        "channel_connections",
        ["tenant_id", "category", "type_id"],
    )

    _seed_types()


def downgrade() -> None:
    op.drop_index("ix_connections_tenant_category_type", table_name="channel_connections")
    op.drop_column("channel_connections", "category")
    op.drop_column("channel_connections", "type_id")
    op.execute(f"drop function if exists {_FUNCTION_SIGNATURE}")
    op.drop_table("connection_types")
