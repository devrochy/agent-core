"""Salud de las conexiones de Telegram (ADR-0199, PR core 2).

`admin_telegram_connection_ids()`, SECURITY DEFINER: el cron de salud (`core.telegram_health_check`) recorre las
conexiones de Telegram de TODOS los negocios y, sin `tenant_scope`, la RLS forzada no le dejaría ver ninguna. Devuelve
solo `(id, tenant_id)` de las conexiones vivas y activas con su fila `telegram_connections` viva; el cron revisa cada
una dentro de su `tenant_scope`. Copia de `admin_twilio_connection_ids()` (0122).

Reversible: el downgrade borra la función.

Revision ID: 0138
Revises: 0137
Create Date: 2026-11-15
"""

from __future__ import annotations

from alembic import op

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION admin_telegram_connection_ids()
        RETURNS TABLE(id uuid, tenant_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT cc.id, cc.tenant_id
            FROM channel_connections cc
            JOIN telegram_connections tc
              ON tc.channel_connection_id = cc.id AND tc.tenant_id = cc.tenant_id
            WHERE cc.provider = 'telegram'
              AND cc.active
              AND cc.deleted_at IS NULL
              AND tc.deleted_at IS NULL;
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION admin_telegram_connection_ids() FROM PUBLIC")
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT EXECUTE ON FUNCTION admin_telegram_connection_ids() TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS admin_telegram_connection_ids()")
