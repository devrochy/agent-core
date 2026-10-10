"""Agent OS · expand 4/5: columnas, constraints y triggers en tablas existentes (HU-AOS-02).

Columnas nuevas, todas nullables o con default constante/estable (PG ≥ 11: sin reescritura):

- `channel_connections` (ADR-0115, ADR-0117 §2): `meta_connection_id`, `settings`,
  `active_release_id`, `canary_release_id`, `canary_percent`, `updated_at`. Sin estado propio para
  Meta: la pausa sigue siendo la de ADR-0122. Las integraciones siguen siendo del negocio (sin
  scope ni `bound_connection_id`, ADR-0115 §3).
- `conversations` (ADR-0117 §2 y §4): `release_id`, `episode_started_at`, `release_lane`,
  `active_agent_instance_id`, `owner_since`.
- `messages`: `turn_id`, `agent_instance_id`.
- `usage_ledger` (ADR-0116 §4, ADR-0117 §5): `kind` (default `'turn'`, las filas viejas lo son),
  `turn_id`, `connection_id`, `agent_instance_id`, `agent_version_id`, `release_id`,
  `a2a_call_id`, `trace_id`.
- `audit_log`: `turn_id`, `connection_id`, `agent_instance_id`.
- `tenants` (ADR-0112): banderas por negocio `aos_runtime` (`legacy` | `v3`) y `aos_procedures`,
  con el CHECK de orden (`aos_procedures` exige `v3`). Tabla de sistema chica: los CHECKs entran
  validados. Va AL FINAL, para no tener `tenants` bloqueada mientras se alteran las demás.

CHECKs y FKs nuevas sobre tablas existentes: `NOT VALID` (no escanean; las valida el contract,
HU-AOS-11/15). Igual se aplican a las filas nuevas, y todas valen para lo que escribe hoy el
runtime: las columnas nuevas nacen NULL o con su default, y `fk_conv_connection` solo exige que
`connection_id` sea una conexión del MISMO negocio (ya lo es: hoy se resuelve dentro del negocio).
`fk_cc_active_release`/`fk_cc_canary_release` cierran el ciclo `channel_connections` ↔
`connection_releases`; `purge_tenant` (0011) desactiva los triggers por tabla, así que el ciclo no
lo afecta.

Triggers del Coordinador (ADR-0112 §1: "una conexión con release activa siempre tiene
Coordinador"), como CONSTRAINT TRIGGER diferidos al COMMIT y con `WHEN`, para que con el runtime
actual (sin releases) no se encole ningún evento:
- `channel_connections_requires_core`: al poner `active_release_id` (o revivir la conexión) debe
  existir una instancia `core` habilitada y viva.
- `agent_instances_core_required`: deshabilitar o borrar (lógico) el core de una conexión con
  release activa se rechaza si no queda otro. No mira la pausa de ADR-0122: una conexión pausada
  conserva su Coordinador, solo que no lo ejecuta.

Fuera de este expand: el contract (índice único de conversación abierta por conexión,
`connection_id` NOT NULL, `VALIDATE`), la memoria por scope (`memory_vectors`, HU-AOS-36: su CHECK
`ck_mv_keys` rechazaría hoy los intercambios que el loop guarda sin `customer_id`) y las columnas de
`eval_runs`/`eval_results` del gate (HU-AOS-21). Sin cambios en los modelos SQLAlchemy: el runtime
no selecciona columnas que todavía no usa, y el `downgrade` no rompe al código desplegado.

`lock_timeout`: cada `ALTER` toma ACCESS EXCLUSIVE por milisegundos; si no lo consigue en 5 s, la
migración falla y se reintenta, en vez de frenar el inbound detrás de una transacción larga.

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-09
"""

from __future__ import annotations

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None

_TRIGGERS_COORDINADOR = """
CREATE OR REPLACE FUNCTION connection_requires_core() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- Se relee la fila: al COMMIT pudo cambiar otra vez dentro de la misma transacción.
  IF EXISTS (
       SELECT 1 FROM channel_connections cc
        WHERE cc.tenant_id = NEW.tenant_id AND cc.id = NEW.id
          AND cc.active_release_id IS NOT NULL AND cc.deleted_at IS NULL)
     AND NOT EXISTS (
       SELECT 1 FROM agent_instances ai
        WHERE ai.tenant_id = NEW.tenant_id AND ai.connection_id = NEW.id
          AND ai.role = 'core' AND ai.enabled AND ai.deleted_at IS NULL) THEN
    RAISE EXCEPTION 'la conexión % tiene release activa sin Coordinador', NEW.id;
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER channel_connections_requires_core
  AFTER INSERT OR UPDATE OF deleted_at, active_release_id ON channel_connections
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW
  WHEN (NEW.active_release_id IS NOT NULL AND NEW.deleted_at IS NULL)
  EXECUTE FUNCTION connection_requires_core();

CREATE OR REPLACE FUNCTION core_instance_required() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (
       SELECT 1 FROM channel_connections cc
        WHERE cc.tenant_id = OLD.tenant_id AND cc.id = OLD.connection_id
          AND cc.active_release_id IS NOT NULL AND cc.deleted_at IS NULL)
     AND NOT EXISTS (
       SELECT 1 FROM agent_instances ai
        WHERE ai.tenant_id = OLD.tenant_id AND ai.connection_id = OLD.connection_id
          AND ai.role = 'core' AND ai.enabled AND ai.deleted_at IS NULL) THEN
    RAISE EXCEPTION 'la conexión % quedaría con release activa sin Coordinador', OLD.connection_id;
  END IF;
  RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER agent_instances_core_required
  AFTER UPDATE OF enabled, deleted_at OR DELETE ON agent_instances
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW
  WHEN (OLD.role = 'core' AND OLD.enabled AND OLD.deleted_at IS NULL)
  EXECUTE FUNCTION core_instance_required();
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    # ------------------------------------------------ channel_connections (ADR-0115, ADR-0117)
    op.execute(
        """
        ALTER TABLE channel_connections
          ADD COLUMN meta_connection_id uuid,
          ADD COLUMN settings           jsonb    NOT NULL DEFAULT '{}'::jsonb,
          ADD COLUMN active_release_id  uuid,
          ADD COLUMN canary_release_id  uuid,
          ADD COLUMN canary_percent     smallint NOT NULL DEFAULT 0,
          ADD COLUMN updated_at         timestamptz NOT NULL DEFAULT now();
        ALTER TABLE channel_connections
          ADD CONSTRAINT ck_cc_canary CHECK (canary_percent BETWEEN 0 AND 50
                                          AND (canary_percent = 0 OR canary_release_id IS NOT NULL)) NOT VALID,
          ADD CONSTRAINT ck_cc_meta_sin_credenciales
                         CHECK (meta_connection_id IS NULL OR credentials_encrypted IS NULL) NOT VALID,
          ADD CONSTRAINT fk_cc_meta FOREIGN KEY (tenant_id, meta_connection_id)
                         REFERENCES meta_connections (tenant_id, id) NOT VALID,
          ADD CONSTRAINT fk_cc_active_release FOREIGN KEY (tenant_id, id, active_release_id)
                         REFERENCES connection_releases (tenant_id, connection_id, id) NOT VALID,
          ADD CONSTRAINT fk_cc_canary_release FOREIGN KEY (tenant_id, id, canary_release_id)
                         REFERENCES connection_releases (tenant_id, connection_id, id) NOT VALID;
        """
    )

    # ------------------------------------------------ conversations (ADR-0117 §2 y §4)
    op.execute(
        """
        ALTER TABLE conversations
          ADD COLUMN release_id               uuid,
          ADD COLUMN episode_started_at       timestamptz,
          ADD COLUMN release_lane             text,
          ADD COLUMN active_agent_instance_id uuid,
          ADD COLUMN owner_since              timestamptz;
        ALTER TABLE conversations
          ADD CONSTRAINT ck_conv_lane CHECK (release_lane IS NULL OR release_lane IN ('stable','canary')) NOT VALID,
          ADD CONSTRAINT fk_conv_connection FOREIGN KEY (tenant_id, connection_id)
                         REFERENCES channel_connections (tenant_id, id) NOT VALID,
          ADD CONSTRAINT fk_conv_release FOREIGN KEY (tenant_id, connection_id, release_id)
                         REFERENCES connection_releases (tenant_id, connection_id, id) NOT VALID,
          ADD CONSTRAINT fk_conv_owner FOREIGN KEY (tenant_id, active_agent_instance_id)
                         REFERENCES agent_instances (tenant_id, id) NOT VALID;
        """
    )

    # ------------------------------------------------ mensajes, uso y auditoría
    op.execute(
        """
        ALTER TABLE messages
          ADD COLUMN turn_id           uuid,
          ADD COLUMN agent_instance_id uuid;
        ALTER TABLE usage_ledger
          ADD COLUMN kind              text NOT NULL DEFAULT 'turn',
          ADD COLUMN turn_id           uuid,
          ADD COLUMN connection_id     uuid,
          ADD COLUMN agent_instance_id uuid,
          ADD COLUMN agent_version_id  uuid,
          ADD COLUMN release_id        uuid,
          ADD COLUMN a2a_call_id       uuid,
          ADD COLUMN trace_id          text;
        ALTER TABLE usage_ledger ADD CONSTRAINT ck_usage_kind
          CHECK (kind IN ('turn','agent_run','classifier','composer','embedding','eval')) NOT VALID;
        ALTER TABLE audit_log
          ADD COLUMN turn_id           uuid,
          ADD COLUMN connection_id     uuid,
          ADD COLUMN agent_instance_id uuid;
        """
    )

    # ------------------------------------------------ Coordinador por conexión (ADR-0112 §1)
    op.execute(_TRIGGERS_COORDINADOR)

    # ------------------------------------------------ banderas por negocio (ADR-0112), al final
    op.execute(
        """
        ALTER TABLE tenants
          ADD COLUMN aos_runtime    text    NOT NULL DEFAULT 'legacy',
          ADD COLUMN aos_procedures boolean NOT NULL DEFAULT false,
          ADD CONSTRAINT ck_tenants_aos_runtime CHECK (aos_runtime IN ('legacy','v3')),
          ADD CONSTRAINT ck_tenants_aos_order   CHECK (NOT aos_procedures OR aos_runtime = 'v3');
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE tenants
          DROP CONSTRAINT IF EXISTS ck_tenants_aos_order,
          DROP CONSTRAINT IF EXISTS ck_tenants_aos_runtime,
          DROP COLUMN IF EXISTS aos_procedures,
          DROP COLUMN IF EXISTS aos_runtime;

        DROP TRIGGER IF EXISTS agent_instances_core_required ON agent_instances;
        DROP TRIGGER IF EXISTS channel_connections_requires_core ON channel_connections;
        DROP FUNCTION IF EXISTS core_instance_required();
        DROP FUNCTION IF EXISTS connection_requires_core();

        ALTER TABLE audit_log
          DROP COLUMN IF EXISTS agent_instance_id,
          DROP COLUMN IF EXISTS connection_id,
          DROP COLUMN IF EXISTS turn_id;
        ALTER TABLE usage_ledger
          DROP CONSTRAINT IF EXISTS ck_usage_kind,
          DROP COLUMN IF EXISTS trace_id,
          DROP COLUMN IF EXISTS a2a_call_id,
          DROP COLUMN IF EXISTS release_id,
          DROP COLUMN IF EXISTS agent_version_id,
          DROP COLUMN IF EXISTS agent_instance_id,
          DROP COLUMN IF EXISTS connection_id,
          DROP COLUMN IF EXISTS turn_id,
          DROP COLUMN IF EXISTS kind;
        ALTER TABLE messages
          DROP COLUMN IF EXISTS agent_instance_id,
          DROP COLUMN IF EXISTS turn_id;

        ALTER TABLE conversations
          DROP CONSTRAINT IF EXISTS fk_conv_owner,
          DROP CONSTRAINT IF EXISTS fk_conv_release,
          DROP CONSTRAINT IF EXISTS fk_conv_connection,
          DROP CONSTRAINT IF EXISTS ck_conv_lane,
          DROP COLUMN IF EXISTS owner_since,
          DROP COLUMN IF EXISTS active_agent_instance_id,
          DROP COLUMN IF EXISTS release_lane,
          DROP COLUMN IF EXISTS episode_started_at,
          DROP COLUMN IF EXISTS release_id;

        ALTER TABLE channel_connections
          DROP CONSTRAINT IF EXISTS fk_cc_canary_release,
          DROP CONSTRAINT IF EXISTS fk_cc_active_release,
          DROP CONSTRAINT IF EXISTS fk_cc_meta,
          DROP CONSTRAINT IF EXISTS ck_cc_meta_sin_credenciales,
          DROP CONSTRAINT IF EXISTS ck_cc_canary,
          DROP COLUMN IF EXISTS updated_at,
          DROP COLUMN IF EXISTS canary_percent,
          DROP COLUMN IF EXISTS canary_release_id,
          DROP COLUMN IF EXISTS active_release_id,
          DROP COLUMN IF EXISTS settings,
          DROP COLUMN IF EXISTS meta_connection_id;
        """
    )
