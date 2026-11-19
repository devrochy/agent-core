"""Procedencia de los registros que crea un agente y lectura de tareas por dueño (HU-AOS-47, ADR-0140).

1. `agent_record_origins`: una fila por registro del negocio que creó una acción del agente
   (pedido, lead, cita, promesa…) con la instancia, versión, conexión, conversación y turno que lo
   crearon (la release sale del turno en `usage_ledger`/`core_decisions`). Los registros viven en tablas de packs: la procedencia se guarda en el
   core para no tocar cada pack ni acoplarlos al Agent OS. `pack_id` + `kind` los declara el pack
   (`MCPTool.crea_registro`); el core no tiene lista cerrada de tipos, solo el formato.
   - Append-only (`prevent_mutation()`, también contra TRUNCATE): solo SELECT e INSERT para
     `app_runtime`. `purge_tenant` (SECURITY DEFINER) la recorre como al resto.
   - RLS por `app.tenant_id` (FORCE), como las tablas de 0069.
   - `UNIQUE (tenant_id, pack_id, kind, record_ref)`: el primero que lo crea gana
     (`ON CONFLICT DO NOTHING`).
   - FKs compuestas con `tenant_id` a `channel_connections`, `agent_instances` (con la conexión:
     instancia y conexión coinciden), `agent_versions` y `conversations`; un CHECK exige la conexión
     si hay instancia (las FKs compuestas son MATCH SIMPLE): la procedencia nunca apunta a otro
     negocio. `turn_id` sin FK: `core_decisions` solo existe con el ledger del Coordinador encendido
     y la procedencia no debe perderse por eso. `label` solo lleva referencias públicas (número de
     pedido), nunca datos del cliente.
2. `ix_conversation_tasks_owner` `(tenant_id, owner_instance_id, updated_at)`, CONCURRENTLY: la
   pestaña Tareas de un agente. No es parcial: el General también lee la rama NULL (tareas de
   antes del runtime v3).

Tabla nueva y vacía: las FKs toman SHARE ROW EXCLUSIVE sobre `conversations`,
`channel_connections`, `agent_instances` y `agent_versions` (frena escrituras, no lecturas) hasta el
COMMIT; se toman al principio, juntas y en el orden de 0069, con `lock_timeout` de 5 s.

**Orden del rollback:** primero el código (el loop escribe la tabla y la API la lee), después el
`downgrade`, que borra la tabla con su historial y el índice.

Revision ID: 0095
Revises: 0094
Create Date: 2026-10-18
"""

from __future__ import annotations

from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None

_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"

_TABLA = """
CREATE TABLE IF NOT EXISTS agent_record_origins (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         uuid NOT NULL,
  pack_id           text NOT NULL CHECK (pack_id ~ '^[a-z0-9_]{1,60}$'),
  kind              text NOT NULL CHECK (kind ~ '^[a-z0-9_]{1,40}$'),
  record_ref        text NOT NULL CHECK (length(record_ref) BETWEEN 1 AND 200),
  label             text CHECK (label IS NULL OR length(label) <= 120),
  tool              text NOT NULL CHECK (length(tool) BETWEEN 1 AND 80),
  agent_instance_id uuid,
  agent_version_id  uuid,
  connection_id     uuid,
  conversation_id   uuid,
  turn_id           uuid,
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_agent_record_origins_ref UNIQUE (tenant_id, pack_id, kind, record_ref),
  CONSTRAINT ck_agent_record_origins_conn
    CHECK (connection_id IS NOT NULL OR agent_instance_id IS NULL),
  CONSTRAINT ck_agent_record_origins_version
    CHECK (agent_instance_id IS NULL OR agent_version_id IS NOT NULL),
  CONSTRAINT fk_agent_record_origins_connection
    FOREIGN KEY (tenant_id, connection_id) REFERENCES channel_connections (tenant_id, id),
  CONSTRAINT fk_agent_record_origins_instance
    FOREIGN KEY (tenant_id, connection_id, agent_instance_id) REFERENCES agent_instances (tenant_id, connection_id, id),
  CONSTRAINT fk_agent_record_origins_version
    FOREIGN KEY (tenant_id, agent_version_id) REFERENCES agent_versions (tenant_id, id),
  CONSTRAINT fk_agent_record_origins_conversation
    FOREIGN KEY (tenant_id, conversation_id) REFERENCES conversations (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS ix_agent_record_origins_agent
  ON agent_record_origins (tenant_id, agent_instance_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_agent_record_origins_conv
  ON agent_record_origins (tenant_id, conversation_id) WHERE conversation_id IS NOT NULL;
"""

# Mismo orden que 0069 (el del consumer) para no cruzarse con un turno en curso.
_LOCK = "LOCK TABLE conversations, channel_connections, agent_instances, agent_versions IN {modo} MODE"

_INDICE = "ix_conversation_tasks_owner"
_INDICE_DEF = "conversation_tasks (tenant_id, owner_instance_id, updated_at)"


def _rls() -> None:
    op.execute("ALTER TABLE agent_record_origins ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_record_origins FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS agent_record_origins_tenant_isolation ON agent_record_origins")
    op.execute(
        "CREATE POLICY agent_record_origins_tenant_isolation ON agent_record_origins "
        f"USING (tenant_id = {_RLS}) WITH CHECK (tenant_id = {_RLS})"
    )
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON agent_record_origins FROM app_runtime;
                GRANT SELECT, INSERT ON agent_record_origins TO app_runtime;
            END IF;
        END $do$;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS agent_record_origins_immutable ON agent_record_origins")
    op.execute(
        "CREATE TRIGGER agent_record_origins_immutable BEFORE UPDATE OR DELETE ON agent_record_origins "
        "FOR EACH ROW EXECUTE FUNCTION prevent_mutation()"
    )
    op.execute("DROP TRIGGER IF EXISTS agent_record_origins_no_truncate ON agent_record_origins")
    op.execute(
        "CREATE TRIGGER agent_record_origins_no_truncate BEFORE TRUNCATE ON agent_record_origins "
        "FOR EACH STATEMENT EXECUTE FUNCTION prevent_mutation()"
    )


def _indice_valido() -> bool | None:
    """None si no existe; False si quedó INVALID (intento interrumpido); True si es válido y es EL
    MISMO índice. Uno homónimo con otra definición no se salta en silencio (patrón de 0071)."""
    fila = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT i.indisvalid, i.indisunique, i.indpred IS NOT NULL, pg_get_indexdef(i.indexrelid) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %(nombre)s",
            {"nombre": _INDICE},
        )
        .first()
    )
    if fila is None:
        return None
    valido, unico, parcial, definicion = fila
    if unico or parcial or "conversation_tasks USING btree (tenant_id, owner_instance_id, updated_at)" not in definicion:
        raise RuntimeError(f"{_INDICE} existe con otra definición: {definicion}")
    return bool(valido)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_LOCK.format(modo="SHARE ROW EXCLUSIVE"))
    op.execute(_TABLA)
    _rls()
    with op.get_context().autocommit_block():
        estado = _indice_valido()
        if estado is False:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDICE}")
        if estado is not True:
            op.execute(f"CREATE INDEX CONCURRENTLY {_INDICE} ON {_INDICE_DEF}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDICE}")
    op.execute("SET LOCAL lock_timeout = '5s'")
    # DROP TABLE quita los triggers RI de los padres con lock exclusivo: mismo orden que el upgrade.
    op.execute(_LOCK.format(modo="ACCESS EXCLUSIVE"))
    op.execute("DROP TABLE IF EXISTS agent_record_origins")
