"""HU-FB-04 · `feedback_pendientes()`: cuántos reportes de clientes esperan revisión, entre negocios.

Revision ID: 0163
Revises: 0162
Create Date: 2026-11-27

La sonda `agentcore_feedback_bandeja_pendientes` (ADR-0188, `core/observability/sondas.py`) mide sin
`tenant_scope`, y `feedback_reports` tiene la RLS forzada. Como `feedback_bandeja` (0162, ADR-0213 §6), es una
función `SECURITY DEFINER` mínima y de solo lectura: devuelve **solo** el estado y la cuenta, nunca ids ni
contenido. Recorre `ix_feedback_reports_bandeja (estado, created_at, id)`.

Reversible: solo crea la función.
"""

from __future__ import annotations

from alembic import op

revision = "0163"
down_revision = "0162"
branch_labels = None
depends_on = None

_FUNCION = "feedback_pendientes()"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION feedback_pendientes()
        RETURNS TABLE(estado text, n bigint)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT r.estado, count(*)
            FROM feedback_reports r
            WHERE r.estado IN ('recibido', 'en_revision')
            GROUP BY r.estado;
        $$;
        REVOKE ALL ON FUNCTION {_FUNCION} FROM PUBLIC;
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT EXECUTE ON FUNCTION {_FUNCION} TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCION}")
