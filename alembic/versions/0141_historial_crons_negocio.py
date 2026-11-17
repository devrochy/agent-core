"""Historial de ejecuciones por negocio (ADR-0200).

Crea `tenant_cron_runs`, tabla de NEGOCIO con RLS forzada. Solo guarda ejecuciones que tuvieron
efecto, fallaron o fueron manuales: un barrido sin efecto no deja fila (serían 34 crons × N
negocios × 96 pasadas/día de ruido). Retención de 90 días.

Dos funciones `SECURITY DEFINER` para el master (patrón ADR-0039: solo metadatos operativos, sin
datos de clientes):
- `admin_tenant_cron_resumen(p_cron)`: por negocio, su configuración y su última ejecución + fallos
  en 24 h, para la vista «Por negocio» de `Plataforma › Crons`.
- `admin_purgar_tenant_cron_runs(p_dias)`: la retención, porque `app_runtime` no puede borrar filas
  de otro negocio bajo RLS.

`app_runtime`: SELECT e INSERT en `tenant_cron_runs`; el borrado va por la función. `tenant_cron_configs`
ya existe (0140).

Revision ID: 0141
Revises: 0140
Create Date: 2026-11-16
"""

from __future__ import annotations

from alembic import op

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None

_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def _tabla() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_cron_runs (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL,
          cron_name text NOT NULL,
          status text NOT NULL,
          trigger text NOT NULL DEFAULT 'scheduled',
          acciones integer NOT NULL DEFAULT 0,
          resumen text,
          error_class text,
          detail text,
          started_at timestamptz NOT NULL DEFAULT now(),
          finished_at timestamptz,
          duration_ms integer,
          CONSTRAINT ck_tenant_cron_runs_status
            CHECK (status IN ('success', 'failed', 'skipped')),
          CONSTRAINT ck_tenant_cron_runs_trigger
            CHECK (trigger IN ('scheduled', 'manual')),
          CONSTRAINT ck_tenant_cron_runs_acciones CHECK (acciones >= 0)
        );
        CREATE INDEX IF NOT EXISTS ix_tenant_cron_runs_cron
          ON tenant_cron_runs (tenant_id, cron_name, started_at DESC);
        CREATE INDEX IF NOT EXISTS ix_tenant_cron_runs_started
          ON tenant_cron_runs (started_at);
        """
    )


def _rls() -> None:
    op.execute("ALTER TABLE tenant_cron_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_cron_runs FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_cron_runs_tenant_isolation ON tenant_cron_runs")
    op.execute(
        "CREATE POLICY tenant_cron_runs_tenant_isolation ON tenant_cron_runs "
        f"USING (tenant_id = {_RLS}) WITH CHECK (tenant_id = {_RLS})"
    )
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON tenant_cron_runs FROM app_runtime;
                GRANT SELECT, INSERT ON tenant_cron_runs TO app_runtime;
            END IF;
        END $do$;
        """
    )


def _funciones() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION admin_tenant_cron_resumen(p_cron text)
        RETURNS TABLE (
          tenant_id uuid,
          habilitada boolean,
          hora_local integer,
          ventana jsonb,
          last_run_at timestamptz,
          last_status text,
          last_resumen text,
          last_acciones integer,
          fallos_24h integer
        )
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
          SELECT c.tenant_id, c.habilitada, c.hora_local, c.ventana,
                 c.last_run_at, c.last_status, c.last_resumen, c.last_acciones,
                 (
                   SELECT count(*)::int
                   FROM tenant_cron_runs r
                   WHERE r.tenant_id = c.tenant_id
                     AND r.cron_name = c.cron_name
                     AND r.status = 'failed'
                     AND r.started_at >= now() - interval '24 hours'
                 ) AS fallos_24h
          FROM tenant_cron_configs c
          WHERE c.cron_name = p_cron AND c.deleted_at IS NULL
        $fn$;
        REVOKE ALL ON FUNCTION admin_tenant_cron_resumen(text) FROM PUBLIC;
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION admin_purgar_tenant_cron_runs(p_dias integer)
        RETURNS integer
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $fn$
          WITH borradas AS (
            DELETE FROM tenant_cron_runs
            WHERE started_at < now() - make_interval(days => p_dias)
            RETURNING 1
          )
          SELECT count(*)::int FROM borradas
        $fn$;
        REVOKE ALL ON FUNCTION admin_purgar_tenant_cron_runs(integer) FROM PUBLIC;
        """
    )
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT EXECUTE ON FUNCTION admin_tenant_cron_resumen(text) TO app_runtime;
                GRANT EXECUTE ON FUNCTION admin_purgar_tenant_cron_runs(integer) TO app_runtime;
            END IF;
        END $do$;
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _tabla()
    _rls()
    _funciones()


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP FUNCTION IF EXISTS admin_purgar_tenant_cron_runs(integer)")
    op.execute("DROP FUNCTION IF EXISTS admin_tenant_cron_resumen(text)")
    op.execute("DROP TABLE IF EXISTS tenant_cron_runs")
