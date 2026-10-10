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
Ambos leen la conexión con `FOR SHARE` (contra el *write skew* de dos transacciones concurrentes
que cumplen cada una por separado) y fallan cerrados si la conexión no es visible (RLS de otro
negocio en contexto): nunca dan por buena una conexión que no pudieron leer.

`conversations`: el dueño tras un handoff es una instancia de ESA conexión (FK
`(tenant_id, connection_id, active_agent_instance_id)`), y `release_id`, `release_lane` y el dueño
exigen `connection_id` (con NULL la FK compuesta no se verificaría). Por eso el camino anterior,
al reescribir `connection_id`, limpia `release_id` y `active_agent_instance_id` (ADR-0115 §6).

`channel_connections.updated_at` nace con la hora de la migración y nadie lo actualiza hasta la HU
que escriba estas columnas (HU-AOS-11/14).

Fuera de este expand: el contract (índice único de conversación abierta por conexión,
`connection_id` NOT NULL, `VALIDATE`), la memoria por scope (`memory_vectors`, HU-AOS-36: su CHECK
`ck_mv_keys` rechazaría hoy los intercambios que el loop guarda sin `customer_id`) y las columnas de
`eval_runs`/`eval_results` del gate (HU-AOS-21). Sin cambios en los modelos SQLAlchemy: el runtime
no selecciona columnas que todavía no usa, y el `downgrade` no rompe al código desplegado.

Locks: cada tabla se altera en SU PROPIA transacción (`autocommit_block`, un `DO` por tabla) con
`lock_timeout` de 5 s. Así nunca hay dos tablas grandes con ACCESS EXCLUSIVE a la vez ni se espera
por una teniendo otra bloqueada (sin deadlocks con el consumer). `tenants` va al final, sola. Es
re-ejecutable: `ADD COLUMN IF NOT EXISTS` y cada constraint o trigger se crea solo si falta.

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


def _alter(tabla: str, columnas: list[str], constraints: dict[str, str]) -> str:
    """Un `DO` (una transacción) que agrega columnas y constraints a UNA tabla, idempotente."""
    sentencias = []
    if columnas:
        adds = ",\n  ".join(f"ADD COLUMN IF NOT EXISTS {c}" for c in columnas)
        sentencias.append(f"ALTER TABLE {tabla}\n  {adds};")
    for nombre, definicion in constraints.items():
        sentencias.append(
            f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{nombre}' "
            f"AND conrelid = '{tabla}'::regclass) THEN\n"
            f"  ALTER TABLE {tabla} ADD CONSTRAINT {nombre} {definicion};\nEND IF;"
        )
    return "DO $$\nBEGIN\n" + "\n".join(sentencias) + "\nEND $$;"


_CAMBIOS: list[tuple[str, list[str], dict[str, str]]] = [
    # ------------------------------------------------ channel_connections (ADR-0115, ADR-0117)
    (
        "channel_connections",
        [
            "meta_connection_id uuid",
            "settings jsonb NOT NULL DEFAULT '{}'::jsonb",
            "active_release_id uuid",
            "canary_release_id uuid",
            "canary_percent smallint NOT NULL DEFAULT 0",
            "updated_at timestamptz NOT NULL DEFAULT now()",
        ],
        {
            "ck_cc_canary": "CHECK (canary_percent BETWEEN 0 AND 50 "
            "AND (canary_percent = 0 OR canary_release_id IS NOT NULL)) NOT VALID",
            # Las filas con cuenta de Meta no guardan credenciales (ADR-0094, ADR-0115 §1).
            "ck_cc_meta_sin_credenciales": "CHECK (meta_connection_id IS NULL "
            "OR credentials_encrypted IS NULL) NOT VALID",
            "fk_cc_meta": "FOREIGN KEY (tenant_id, meta_connection_id) "
            "REFERENCES meta_connections (tenant_id, id) NOT VALID",
            # El puntero de la conexión solo apunta a SUS releases.
            "fk_cc_active_release": "FOREIGN KEY (tenant_id, id, active_release_id) "
            "REFERENCES connection_releases (tenant_id, connection_id, id) NOT VALID",
            "fk_cc_canary_release": "FOREIGN KEY (tenant_id, id, canary_release_id) "
            "REFERENCES connection_releases (tenant_id, connection_id, id) NOT VALID",
        },
    ),
    # ------------------------------------------------ conversations (ADR-0117 §2 y §4)
    (
        "conversations",
        [
            "release_id uuid",
            "episode_started_at timestamptz",
            "release_lane text",
            "active_agent_instance_id uuid",
            "owner_since timestamptz",
        ],
        {
            "ck_conv_lane": "CHECK (release_lane IS NULL OR "
            "(release_lane IN ('stable','canary') AND release_id IS NOT NULL)) NOT VALID",
            "ck_conv_aos_requiere_conexion": "CHECK (connection_id IS NOT NULL "
            "OR (release_id IS NULL AND active_agent_instance_id IS NULL)) NOT VALID",
            "fk_conv_connection": "FOREIGN KEY (tenant_id, connection_id) "
            "REFERENCES channel_connections (tenant_id, id) NOT VALID",
            "fk_conv_release": "FOREIGN KEY (tenant_id, connection_id, release_id) "
            "REFERENCES connection_releases (tenant_id, connection_id, id) NOT VALID",
            "fk_conv_owner": "FOREIGN KEY (tenant_id, connection_id, active_agent_instance_id) "
            "REFERENCES agent_instances (tenant_id, connection_id, id) NOT VALID",
        },
    ),
    # ------------------------------------------------ mensajes, uso y auditoría
    ("messages", ["turn_id uuid", "agent_instance_id uuid"], {}),
    (
        "usage_ledger",
        [
            "kind text NOT NULL DEFAULT 'turn'",
            "turn_id uuid",
            "connection_id uuid",
            "agent_instance_id uuid",
            "agent_version_id uuid",
            "release_id uuid",
            "a2a_call_id uuid",
            "trace_id text",
        ],
        {
            "ck_usage_kind": "CHECK (kind IN ('turn','agent_run','classifier','composer',"
            "'embedding','eval')) NOT VALID",
        },
    ),
    ("audit_log", ["turn_id uuid", "connection_id uuid", "agent_instance_id uuid"], {}),
]

_TRIGGERS_COORDINADOR = """
CREATE OR REPLACE FUNCTION connection_requires_core() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_release uuid;
  v_deleted timestamptz;
BEGIN
  -- Se relee la fila al COMMIT (pudo cambiar dentro de la transacción) y se bloquea en modo
  -- compartido contra el write skew con agent_instances_core_required.
  SELECT cc.active_release_id, cc.deleted_at INTO v_release, v_deleted
    FROM channel_connections cc
   WHERE cc.tenant_id = NEW.tenant_id AND cc.id = NEW.id
     FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'conexión % no visible al validar su Coordinador (¿otro negocio en contexto?)', NEW.id;
  END IF;
  IF v_release IS NOT NULL AND v_deleted IS NULL AND NOT EXISTS (
       SELECT 1 FROM agent_instances ai
        WHERE ai.tenant_id = NEW.tenant_id AND ai.connection_id = NEW.id
          AND ai.role = 'core' AND ai.enabled AND ai.deleted_at IS NULL) THEN
    RAISE EXCEPTION 'la conexión % tiene release activa sin Coordinador', NEW.id;
  END IF;
  RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION core_instance_required() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_release uuid;
  v_deleted timestamptz;
BEGIN
  SELECT cc.active_release_id, cc.deleted_at INTO v_release, v_deleted
    FROM channel_connections cc
   WHERE cc.tenant_id = OLD.tenant_id AND cc.id = OLD.connection_id
     FOR SHARE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'conexión % no visible al validar su Coordinador (¿otro negocio en contexto?)',
      OLD.connection_id;
  END IF;
  IF v_release IS NOT NULL AND v_deleted IS NULL AND NOT EXISTS (
       SELECT 1 FROM agent_instances ai
        WHERE ai.tenant_id = OLD.tenant_id AND ai.connection_id = OLD.connection_id
          AND ai.role = 'core' AND ai.enabled AND ai.deleted_at IS NULL) THEN
    RAISE EXCEPTION 'la conexión % quedaría con release activa sin Coordinador', OLD.connection_id;
  END IF;
  RETURN NULL;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'channel_connections_requires_core') THEN
    CREATE CONSTRAINT TRIGGER channel_connections_requires_core
      AFTER INSERT OR UPDATE OF deleted_at, active_release_id ON channel_connections
      DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW
      WHEN (NEW.active_release_id IS NOT NULL AND NEW.deleted_at IS NULL)
      EXECUTE FUNCTION connection_requires_core();
  END IF;
END $$;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'agent_instances_core_required') THEN
    CREATE CONSTRAINT TRIGGER agent_instances_core_required
      AFTER UPDATE OF enabled, deleted_at OR DELETE ON agent_instances
      DEFERRABLE INITIALLY DEFERRED
      FOR EACH ROW
      WHEN (OLD.role = 'core' AND OLD.enabled AND OLD.deleted_at IS NULL)
      EXECUTE FUNCTION core_instance_required();
  END IF;
END $$;
"""

# Banderas por negocio (ADR-0112). tenants es chica: los CHECK entran validados.
_TENANTS = _alter(
    "tenants",
    ["aos_runtime text NOT NULL DEFAULT 'legacy'", "aos_procedures boolean NOT NULL DEFAULT false"],
    {
        "ck_tenants_aos_runtime": "CHECK (aos_runtime IN ('legacy','v3'))",
        "ck_tenants_aos_order": "CHECK (NOT aos_procedures OR aos_runtime = 'v3')",
    },
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        for tabla, columnas, constraints in _CAMBIOS:
            op.execute(_alter(tabla, columnas, constraints))
        op.execute(_TRIGGERS_COORDINADOR)
        op.execute(_TENANTS)
        op.execute("RESET lock_timeout")


def _drop(tabla: str, columnas: list[str], constraints: dict[str, str]) -> str:
    partes = [f"DROP CONSTRAINT IF EXISTS {n}" for n in reversed(list(constraints))]
    partes += [f"DROP COLUMN IF EXISTS {c.split()[0]}" for c in reversed(columnas)]
    return f"ALTER TABLE {tabla} " + ", ".join(partes)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute(
            _drop(
                "tenants",
                ["aos_runtime", "aos_procedures"],
                {"ck_tenants_aos_runtime": "", "ck_tenants_aos_order": ""},
            )
        )
        op.execute(
            """
            DROP TRIGGER IF EXISTS agent_instances_core_required ON agent_instances;
            DROP TRIGGER IF EXISTS channel_connections_requires_core ON channel_connections;
            DROP FUNCTION IF EXISTS core_instance_required();
            DROP FUNCTION IF EXISTS connection_requires_core();
            """
        )
        for tabla, columnas, constraints in reversed(_CAMBIOS):
            op.execute(_drop(tabla, columnas, constraints))
        op.execute("RESET lock_timeout")
