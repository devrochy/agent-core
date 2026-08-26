"""Multi-tenant core (ADR-0001/0007/0008/0012/0013).

- Tablas nuevas: tenants, tenant_user (sistema), processed_events, usage_ledger.
- tenant_id + RLS + policy + índice por tenant en TODAS las tablas de negocio.
- UNIQUEs globales → compuestos por tenant con parcial `WHERE deleted_at IS NULL`.
- Funciones SECURITY DEFINER para resolver el tenant sin RLS (bootstrap, ADR-0012).
- Backfill: crea el tenant #1 y adosa las filas existentes.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-26
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# tablas de negocio nuevas que YA llevan tenant_id
NEW_BUSINESS_TABLES = ["processed_events", "usage_ledger"]

# tablas de negocio existentes que necesitan la columna tenant_id
EXISTING_BUSINESS_TABLES = [
    "app_settings",
    "customers",
    "channel_connections",
    "conversations",
    "messages",
    "plugin_configs",
    "escalations",
    "llm_usage",
    "audit_log",
    "memory_vectors",
]

BUSINESS_TABLES = EXISTING_BUSINESS_TABLES + NEW_BUSINESS_TABLES

# tablas de negocio con soft-delete (audit_log y usage_ledger son inmutables)
SOFT_DELETE_TABLES = [
    "app_settings",
    "customers",
    "channel_connections",
    "conversations",
    "messages",
    "plugin_configs",
    "escalations",
    "llm_usage",
    "memory_vectors",
]


def upgrade() -> None:
    # ---------- 1. Tablas nuevas de sistema (sin RLS) ----------
    op.create_table(
        "tenants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("settings", JSONB(), nullable=False, server_default="{}"),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_unique_constraint("uq_tenants_slug", "tenants", ["slug"])

    op.create_table(
        "tenant_user",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("admin_id", UUID(as_uuid=True), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="owner"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_unique_constraint("uq_tenant_user", "tenant_user", ["tenant_id", "admin_id"])

    # ---------- 2. Tablas nuevas de negocio ----------
    op.create_table(
        "processed_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("external_message_id", sa.Text(), nullable=False),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_processed_events_tenant", "processed_events", ["tenant_id", "external_message_id"])
    op.execute(
        "create unique index if not exists uq_processed_event_tenant_msg "
        "on processed_events (tenant_id, external_message_id) where deleted_at is null"
    )

    op.create_table(
        "usage_ledger",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("tool_calls", JSONB(), nullable=True),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_usage_ledger_tenant_created", "usage_ledger", ["tenant_id", "created_at"])
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_usage_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'usage_ledger es append-only (solo INSERT)';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER usage_ledger_immutable
        BEFORE UPDATE OR DELETE ON usage_ledger
        FOR EACH ROW EXECUTE FUNCTION prevent_usage_mutation();
        """
    )

    # ---------- 3. Columnas tenant_id + deleted_at en tablas de negocio ----------
    for table in EXISTING_BUSINESS_TABLES:
        op.add_column(table, sa.Column("tenant_id", UUID(as_uuid=True), nullable=True))

    for table in SOFT_DELETE_TABLES:
        op.add_column(table, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    # ---------- 4. Backfill: tenant #1 y adosar filas existentes ----------
    op.execute(
        """
        INSERT INTO tenants (id, name, slug)
        SELECT '00000000-0000-0000-0000-000000000001', 'Tenant #1', 'tenant-1'
        WHERE NOT EXISTS (SELECT 1 FROM tenants WHERE slug = 'tenant-1')
        """
    )
    op.execute("update tenants set slug = 'tenant-1' where id = '00000000-0000-0000-0000-000000000001'")

    for table in BUSINESS_TABLES:
        op.execute(
            f"update {table} set tenant_id = '00000000-0000-0000-0000-000000000001' where tenant_id is null"
        )

    # app_settings: única fila por tenant
    op.create_unique_constraint("uq_app_settings_tenant", "app_settings", ["tenant_id"])

    # plugin_configs: PK compuesto (plugin_id, tenant_id) — config por tenant
    op.execute("alter table plugin_configs drop constraint if exists plugin_configs_pkey")
    op.execute("alter table plugin_configs add primary key (plugin_id, tenant_id)")

    for table in BUSINESS_TABLES:
        op.alter_column(table, "tenant_id", nullable=False)

    # ---------- 5. UNIQUEs globales → compuestos por tenant con parcial ----------
    op.execute("alter table customers drop constraint if exists uq_customer_channel_external")
    op.execute(
        "create unique index if not exists uq_customer_tenant_channel_external "
        "on customers (tenant_id, channel, external_id) where deleted_at is null"
    )
    op.execute("alter table channel_connections drop constraint if exists uq_connection_provider_external")
    op.execute(
        "create unique index if not exists uq_connection_tenant_provider_external "
        "on channel_connections (tenant_id, provider, external_id) where deleted_at is null"
    )
    op.execute("alter table messages drop constraint if exists messages_idempotency_key_key")
    op.execute(
        "create unique index if not exists uq_messages_tenant_idempotency "
        "on messages (tenant_id, idempotency_key) where deleted_at is null and idempotency_key is not null"
    )

    # ---------- 6. Índices compuestos por tenant ----------
    op.create_index("ix_app_settings_tenant", "app_settings", ["tenant_id"])
    op.create_index("ix_customers_tenant_created", "customers", ["tenant_id", "created_at"])
    op.create_index("ix_connections_tenant", "channel_connections", ["tenant_id"])
    op.create_index("ix_conversations_tenant_created", "conversations", ["tenant_id", "started_at"])
    op.create_index("ix_messages_tenant_conv_created", "messages", ["tenant_id", "conversation_id", "created_at"])
    op.create_index("ix_plugin_configs_tenant", "plugin_configs", ["tenant_id"])
    op.create_index("ix_escalations_tenant_status", "escalations", ["tenant_id", "status"])
    op.create_index("ix_llm_usage_tenant_created", "llm_usage", ["tenant_id", "created_at"])
    op.create_index("ix_audit_log_tenant_created", "audit_log", ["tenant_id", "created_at"])
    op.create_index("ix_memory_vectors_tenant_customer", "memory_vectors", ["tenant_id", "customer_id"])

    # ---------- 7. RLS + policy de aislamiento ----------
    # `current_setting(..., true)` devuelve NULL si la variable no existe; nullif()
    # convierte el '' (variable sin setear) en NULL. Así, sin contexto de tenant la
    # comparación da NULL -> 0 filas (fail-closed), en vez de error de tipo uuid.
    for table in BUSINESS_TABLES:
        op.execute(f"alter table {table} enable row level security")
        op.execute(
            f"create policy tenant_isolation on {table} using "
            "(tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) "
            "with check (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
        )

    # ---------- 8. SECURITY DEFINER (bootstrap chicken-and-egg, ADR-0012) ----------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION resolve_tenant_for_phone(p_phone_number_id text)
        RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT tenant_id FROM channel_connections
            WHERE external_id = p_phone_number_id AND deleted_at IS NULL
            LIMIT 1;
        $$;

        CREATE OR REPLACE FUNCTION admin_tenants(p_admin_id uuid)
        RETURNS TABLE(tenant_id uuid, role text)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT tu.tenant_id, tu.role FROM tenant_user tu WHERE tu.admin_id = p_admin_id;
        $$;

        CREATE OR REPLACE FUNCTION default_tenant_id()
        RETURNS uuid LANGUAGE sql STABLE AS $$
            SELECT id FROM tenants WHERE deleted_at IS NULL ORDER BY created_at LIMIT 1;
        $$;
        """
    )

    # ---------- 9. Rol de runtime: RLS como última línea de defensa ----------
    # El dueño de las tablas omite RLS. El runtime conecta como app_runtime (NO dueño),
    # así las policies sí se aplican. Migraciones y bootstrap corren como el dueño.
    # Contraseña por defecto de desarrollo (mismo patrón que agent_core:agent_core);
    # producción la fija por env.
    op.execute(
        """
        DO $do$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                CREATE ROLE app_runtime LOGIN PASSWORD 'app_runtime';
            END IF;
        END
        $do$;

        GRANT USAGE ON SCHEMA public TO app_runtime;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_runtime;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_runtime;
        GRANT USAGE ON TYPE vector TO app_runtime;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT USAGE, SELECT ON SEQUENCES TO app_runtime;
        """
    )


def downgrade() -> None:
    # ---------- 1. RLS y policies ----------
    for table in BUSINESS_TABLES:
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.execute(f"alter table {table} disable row level security")

    # ---------- 2. Funciones SECURITY DEFINER y triggers ----------
    op.execute("drop function if exists resolve_tenant_for_phone(text)")
    op.execute("drop function if exists admin_tenants(uuid)")
    op.execute("drop function if exists default_tenant_id()")
    op.execute("drop trigger if exists usage_ledger_immutable ON usage_ledger")
    op.execute("drop function if exists prevent_usage_mutation")

    # ---------- 3. UNIQUEs nuevos → restaurar globales ----------
    op.execute("drop index if exists uq_customer_tenant_channel_external")
    op.execute("alter table customers add constraint uq_customer_channel_external unique (channel, external_id)")
    op.execute("drop index if exists uq_connection_tenant_provider_external")
    op.execute(
        "alter table channel_connections add constraint uq_connection_provider_external unique (provider, external_id)"
    )
    op.execute("drop index if exists uq_messages_tenant_idempotency")
    op.execute("create unique index if not exists messages_idempotency_key_key on messages (idempotency_key)")

    op.execute("alter table app_settings drop constraint if exists uq_app_settings_tenant")
    op.execute("alter table plugin_configs drop constraint if exists plugin_configs_pkey")
    op.execute("alter table plugin_configs add primary key (plugin_id)")

    # ---------- 4. Índices ----------
    for index in [
        "ix_app_settings_tenant",
        "ix_customers_tenant_created",
        "ix_connections_tenant",
        "ix_conversations_tenant_created",
        "ix_messages_tenant_conv_created",
        "ix_plugin_configs_tenant",
        "ix_escalations_tenant_status",
        "ix_llm_usage_tenant_created",
        "ix_audit_log_tenant_created",
        "ix_memory_vectors_tenant_customer",
        "ix_processed_events_tenant",
        "ix_usage_ledger_tenant_created",
    ]:
        op.execute(f"drop index if exists {index}")

    # ---------- 5. Columnas ----------
    for table in SOFT_DELETE_TABLES:
        op.drop_column(table, "deleted_at")
    for table in BUSINESS_TABLES:
        op.drop_column(table, "tenant_id")

    # ---------- 6. Tablas nuevas ----------
    op.drop_table("usage_ledger")
    op.drop_table("processed_events")
    op.drop_table("tenant_user")
    op.drop_table("tenants")

    # ---------- 7. Rol de runtime ----------
    op.execute(
        "DO $do$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN "
        "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_runtime; "
        "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM app_runtime; "
        "REVOKE ALL ON TYPE vector FROM app_runtime; "
        "REVOKE USAGE ON SCHEMA public FROM app_runtime; END IF; END $do$"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM app_runtime"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM app_runtime"
    )
    op.execute("DROP ROLE IF EXISTS app_runtime")