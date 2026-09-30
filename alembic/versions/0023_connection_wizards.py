"""connection_wizards: máquina de estados persistida del wizard de Embedded
Signup (WhatsApp), reanudable si el admin cierra el navegador a mitad de
camino.

Tabla de NEGOCIO normal (pertenece a un tenant, a diferencia de
`webhook_events`/`meta_app_subscriptions`): `tenant_id not null` + RLS,
mismo patrón que `meta_connections` (migración 0014). Ver
docs/adr/0030-connection-wizards-maquina-estados.md -- regla dura: `context`/
`last_error` nunca contienen `code` ni tokens (columnas legibles por RLS del
propio tenant, no un lugar para secretos).

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-22
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

WIZARD_STEPS = (
    "select_channel",
    "launch_oauth",
    "exchange_code",
    "discover_assets",
    "select_assets",
    "subscribe_webhook",
    "register_number",
    "sync_templates",
    "verify_health",
    "done",
)


def upgrade() -> None:
    op.create_table(
        "connection_wizards",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", UUID(as_uuid=True), sa.ForeignKey("meta_connections.id", ondelete="CASCADE"), nullable=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("environment", sa.Text(), nullable=False, server_default="production"),
        sa.Column("current_step", sa.Text(), nullable=False, server_default="select_channel"),
        sa.Column("completed_steps", ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("context", JSONB(), nullable=False, server_default="{}"),
        sa.Column("last_error", JSONB(), nullable=True),
        sa.Column("started_by", UUID(as_uuid=True), sa.ForeignKey("admins.id", ondelete="SET NULL"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now() + interval '24 hours'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "channel in ('whatsapp','messenger','instagram','facebook_page')",
            name="ck_connection_wizards_channel",
        ),
        sa.CheckConstraint("environment in ('sandbox','production')", name="ck_connection_wizards_environment"),
        sa.CheckConstraint(
            "current_step in (" + ",".join(f"'{s}'" for s in WIZARD_STEPS) + ")",
            name="ck_connection_wizards_current_step",
        ),
    )
    op.create_index("ix_connection_wizards_tenant", "connection_wizards", ["tenant_id"])
    # Un solo wizard "vivo" por tenant+canal+entorno a la vez -- reanudar
    # significa reusar la fila existente, no crear una segunda en paralelo.
    op.create_index(
        "uq_connection_wizards_tenant_channel_env_activo",
        "connection_wizards",
        ["tenant_id", "channel", "environment"],
        unique=True,
        postgresql_where=sa.text("current_step != 'done'"),
    )

    op.execute("alter table connection_wizards enable row level security")
    op.execute(
        "create policy tenant_isolation on connection_wizards using "
        "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
        "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )
    op.execute("grant select, insert, update, delete on connection_wizards to app_runtime")


def downgrade() -> None:
    op.execute("drop policy if exists tenant_isolation on connection_wizards")
    op.execute("alter table connection_wizards disable row level security")
    op.execute("drop index if exists uq_connection_wizards_tenant_channel_env_activo")
    op.execute("drop index if exists ix_connection_wizards_tenant")
    op.drop_table("connection_wizards")
