"""Funciones SECURITY DEFINER para enumerar assets de Meta cross-tenant.

Los crons de Meta (`meta_subscriptions_health`, `message_template_sync`,
`meta_pricing_sync`, `meta_health_check`) enumeran conexiones/WABAs SIN
`tenant_scope` para poder recorrer TODOS los tenants. Con RLS fail-closed esas
queries devuelven 0 filas -> el cron corría como no-op silencioso.

Mismo patrón que `resolve_tenant_for_phone`/`admin_tenants` (migración 0003,
ADR-0012): una función SECURITY DEFINER (dueño) que omite RLS para el
bootstrap. Devuelve SOLO `id` + `tenant_id` (nunca secretos); cada cron
hidrata/opera el asset bajo el `tenant_scope` de su tenant.

Revision ID: 0033
Revises: 0032
Create Date: 2026-09-22
"""
from __future__ import annotations

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION admin_meta_connection_ids(p_statuses text[])
        RETURNS TABLE(id uuid, tenant_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT m.id, m.tenant_id
            FROM meta_connections m
            WHERE m.status = ANY(p_statuses) AND m.deleted_at IS NULL;
        $$;

        CREATE OR REPLACE FUNCTION admin_connected_waba_ids()
        RETURNS TABLE(id uuid, tenant_id uuid)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT w.id, w.tenant_id
            FROM waba_accounts w
            JOIN meta_connections c ON c.id = w.connection_id AND c.tenant_id = w.tenant_id
            WHERE c.status = 'connected' AND c.deleted_at IS NULL;
        $$;

        -- No exponer el catálogo cross-tenant a PUBLIC: solo el rol de runtime
        -- (app_runtime) las necesita. El dueño (rol de migraciones) conserva
        -- EXECUTE por ser owner.
        REVOKE ALL ON FUNCTION admin_meta_connection_ids(text[]) FROM PUBLIC;
        REVOKE ALL ON FUNCTION admin_connected_waba_ids() FROM PUBLIC;
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT EXECUTE ON FUNCTION admin_meta_connection_ids(text[]) TO app_runtime;
                GRANT EXECUTE ON FUNCTION admin_connected_waba_ids() TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("drop function if exists admin_meta_connection_ids(text[])")
    op.execute("drop function if exists admin_connected_waba_ids()")
