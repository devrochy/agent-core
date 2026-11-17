"""Crons por negocio (ADR-0200): política por tenant de las automatizaciones.

Crea `tenant_cron_configs`, tabla de NEGOCIO con RLS forzada: una fila por negocio y cron
configurado. Sin fila = valores por defecto del catálogo (un cron de pack está encendido si el
pack lo está; uno de core, siempre). Guarda el encendido/apagado, la hora local, la ventana
horaria y la última ejecución que la pantalla muestra en la tarjeta.

No guarda datos de clientes ni mensajes: solo configuración operativa y un resumen corto de la
última ejecución («12 recordatorios enviados»). El historial detallado va en `tenant_cron_runs`
(0141), solo con ejecuciones que tuvieron efecto.

Permisos de `app_runtime`: SELECT, INSERT y UPDATE. Sin DELETE: apagar/reiniciar es borrado
lógico (`deleted_at`), invariante 8. Solo la toca `core/admin/crons_negocio_service.py`.

Revision ID: 0142
Revises: 0141
Create Date: 2026-11-16
"""

from __future__ import annotations

from alembic import op

revision = "0142"
down_revision = "0141"
branch_labels = None
depends_on = None

_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"


def _tabla() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tenant_cron_configs (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL,
          cron_name text NOT NULL,
          habilitada boolean NOT NULL DEFAULT true,
          hora_local integer,
          ventana jsonb,
          last_run_at timestamptz,
          last_status text,
          last_resumen text,
          last_acciones integer,
          updated_by uuid,
          updated_at timestamptz NOT NULL DEFAULT now(),
          deleted_at timestamptz,
          CONSTRAINT uq_tenant_cron_configs UNIQUE (tenant_id, cron_name),
          CONSTRAINT ck_tenant_cron_configs_hora
            CHECK (hora_local IS NULL OR (hora_local >= 0 AND hora_local <= 23)),
          CONSTRAINT ck_tenant_cron_configs_status
            CHECK (last_status IS NULL OR last_status IN ('success', 'failed', 'skipped'))
        );
        CREATE INDEX IF NOT EXISTS ix_tenant_cron_configs_tenant
          ON tenant_cron_configs (tenant_id);
        """
    )


def _rls() -> None:
    op.execute("ALTER TABLE tenant_cron_configs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_cron_configs FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_cron_configs_tenant_isolation ON tenant_cron_configs")
    op.execute(
        "CREATE POLICY tenant_cron_configs_tenant_isolation ON tenant_cron_configs "
        f"USING (tenant_id = {_RLS}) WITH CHECK (tenant_id = {_RLS})"
    )
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON tenant_cron_configs FROM app_runtime;
                GRANT SELECT, INSERT, UPDATE ON tenant_cron_configs TO app_runtime;
            END IF;
        END $do$;
        """
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _tabla()
    _rls()


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP TABLE IF EXISTS tenant_cron_configs")
