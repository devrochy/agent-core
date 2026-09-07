"""Ciclo de vida de tenants: purga física + suspensión (ADR-0001).

- `purge_tenant(tid)`: borra TODOS los datos del tenant (core + packs, vía
  descubrimiento dinámico de tablas con `tenant_id`) y la fila `tenants`.
  Es la ÚNICA excepción a la invariante #8 (sin borrado físico): operación
  irreversible, master-only, confirmada por frase en el CLI/panel (tipo GDPR).
  SECURITY DEFINER (dueño) → omite RLS. Desactiva triggers por tabla (cubre FKs
  e inmutables audit_log/usage_ledger) dentro de una transacción: si algo falla,
  rollback y los triggers vuelven solos.
- `default_tenant()` pasa a omitir tenants suspendidos (fallback de desarrollo).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-05
"""
from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION purge_tenant(p_tenant_id uuid)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public
        AS $$
        DECLARE
            tbl text;
            rec record;
        BEGIN
            -- Descubre dinámicamente toda tabla de negocio (core + packs) con tenant_id.
            FOR rec IN
                SELECT table_name
                FROM information_schema.columns
                WHERE column_name = 'tenant_id' AND table_schema = 'public'
            LOOP
                tbl := quote_ident(rec.table_name);
                -- Disable triggers (FKs e inmutables audit_log/usage_ledger): así el
                -- orden de borrado no importa y no hay violación de constraints.
                EXECUTE format('ALTER TABLE %s DISABLE TRIGGER ALL', tbl);
                EXECUTE format('DELETE FROM %s WHERE tenant_id = $1', tbl) USING p_tenant_id;
                EXECUTE format('ALTER TABLE %s ENABLE TRIGGER ALL', tbl);
            END LOOP;

            DELETE FROM tenant_user WHERE tenant_id = p_tenant_id;
            DELETE FROM tenants WHERE id = p_tenant_id;
        END;
        $$;
        """
    )

    # El fallback de desarrollo no debe recaer en un tenant suspendido.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION default_tenant_id()
        RETURNS uuid LANGUAGE sql STABLE AS $$
            SELECT id FROM tenants
            WHERE deleted_at IS NULL AND status = 'active'
            ORDER BY created_at LIMIT 1;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("drop function if exists purge_tenant(uuid)")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION default_tenant_id()
        RETURNS uuid LANGUAGE sql STABLE AS $$
            SELECT id FROM tenants WHERE deleted_at IS NULL ORDER BY created_at LIMIT 1;
        $$;
        """
    )