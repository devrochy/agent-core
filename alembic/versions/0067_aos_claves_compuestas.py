"""Agent OS · expand 1/5: claves `(tenant_id, id)` y `prevent_mutation()` (HU-AOS-02, ADR-0114/0116).

- `UNIQUE (tenant_id, id)` en `channel_connections`, `conversations`, `customers` y
  `meta_connections`: destino de las FKs compuestas del Agent OS (una referencia entre negocios
  es imposible a nivel de esquema, ADR-0116 §4). Son tablas grandes: el índice se crea con
  `CREATE UNIQUE INDEX CONCURRENTLY` fuera de la transacción y después se ata como constraint
  con `ADD CONSTRAINT … USING INDEX` (lock breve, sin escaneo).
- `UNIQUE (tenant_id, id)` también en `agent_specs` y `eval_runs` (chicas), destino de las FKs
  compuestas de `agent_versions.legacy_agent_spec_id` e `improvement_proposals.eval_run_id`.
- `prevent_mutation()`: función genérica nueva (ADR-0114 §1). Rechaza DELETE, TRUNCATE y todo
  UPDATE salvo las columnas que recibe como argumento del trigger (`TG_ARGV`), con `search_path`
  fijo. Sin argumentos, la tabla es
  append-only estricta. `purge_tenant` (0011) la salta al desactivar los triggers.

Sin cambio de conducta: nada en runtime usa la función ni las constraints nuevas (el `id` ya es
único, así que `(tenant_id, id)` también lo es).

Todo lo que toca tablas grandes corre fuera de la transacción de la migración
(`autocommit_block`): el índice concurrente y después cada `ADD CONSTRAINT` como sentencia propia,
con `lock_timeout` de sesión. Así cada tabla toma su ACCESS EXCLUSIVE de milisegundos y lo suelta
enseguida, sin acumular locks de varias tablas (el consumer escribe `customers` y luego
`conversations`: retenerlas juntas podía cerrar un deadlock). Es re-ejecutable: un índice válido
se salta, uno INVALID de un intento anterior se rehace y la constraint se ata solo si falta.

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-09
"""

from __future__ import annotations

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None

# tabla → (índice, constraint)
_CLAVES = {
    "channel_connections": ("uq_cc_tenant_id", "uq_channel_connections_tenant_id"),
    "conversations": ("uq_conv_tenant_id", "uq_conversations_tenant_id"),
    "customers": ("uq_cust_tenant_id", "uq_customers_tenant_id"),
    "meta_connections": ("uq_meta_tenant_id", "uq_meta_connections_tenant_id"),
    # Destinos de FKs compuestas desde tablas nuevas (ADR-0116 §4): versión legacy y corrida de evals.
    "agent_specs": ("uq_agent_specs_tenant_id_idx", "uq_agent_specs_tenant_id"),
    "eval_runs": ("uq_eval_runs_tenant_id_idx", "uq_eval_runs_tenant_id"),
}
# índice → (tabla, único, columnas, parcial)
_ESPERADO = {
    indice: (tabla, True, "(tenant_id, id)", False) for tabla, (indice, _c) in _CLAVES.items()
}

_PREVENT_MUTATION = """
CREATE OR REPLACE FUNCTION prevent_mutation() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    v_old jsonb;
    v_new jsonb;
    v_col text;
BEGIN
    -- TRUNCATE llega por un trigger BEFORE TRUNCATE ... FOR EACH STATEMENT (sin OLD/NEW).
    IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
        RAISE EXCEPTION '% es append-only (%)', TG_TABLE_NAME, TG_OP;
    END IF;
    v_old := to_jsonb(OLD);
    v_new := to_jsonb(NEW);
    IF TG_NARGS > 0 THEN
        FOREACH v_col IN ARRAY TG_ARGV LOOP  -- columnas exentas, p. ej. 'yanked'
            v_old := v_old - v_col;
            v_new := v_new - v_col;
        END LOOP;
    END IF;
    IF v_old IS DISTINCT FROM v_new THEN
        RAISE EXCEPTION '% es append-only (UPDATE fuera de las columnas exentas)', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END $$;
"""


def _indice_existente(nombre: str) -> bool | None:
    """None si no existe; False si quedó INVALID; True si existe válido y es EL MISMO índice.

    Un índice con el mismo nombre pero otra tabla, columnas, unicidad o parcialidad no se salta en
    silencio: la migración falla para que alguien lo mire.
    """
    tabla, unico, columnas, parcial = _ESPERADO[nombre]
    fila = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT i.indisvalid, t.relname, i.indisunique, pg_get_indexdef(i.indexrelid), "
            "i.indpred IS NOT NULL "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %(nombre)s",
            {"nombre": nombre},
        )
        .first()
    )
    if fila is None:
        return None
    valido, tabla_real, unico_real, definicion, parcial_real = fila
    if not valido:
        return False
    if (tabla_real, unico_real, parcial_real) != (tabla, unico, parcial) or (
        f"USING btree {columnas}" not in definicion
    ):
        raise RuntimeError(f"Ya existe un índice {nombre} distinto del esperado: {definicion}")
    return True


def _indice_unico_concurrente(indice: str, tabla: str, constraint: str) -> None:
    """Crea el índice sin bloquear escrituras; idempotente ante un intento fallido."""
    # `ADD CONSTRAINT … USING INDEX` renombra el índice al nombre de la constraint: si la
    # constraint ya existe, el índice también (con otro nombre) y no hay nada que crear.
    ya_atado = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT 1 FROM pg_constraint WHERE conname = %(c)s AND conrelid = %(t)s::regclass",
            {"c": constraint, "t": f"public.{tabla}"},
        )
        .first()
    )
    if ya_atado:
        return
    estado = _indice_existente(indice)
    if estado:
        return
    if estado is False:  # quedó INVALID de un CONCURRENTLY interrumpido
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {indice}")
    op.execute(f"CREATE UNIQUE INDEX CONCURRENTLY {indice} ON {tabla} (tenant_id, id)")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for tabla, (indice, constraint) in _CLAVES.items():
            _indice_unico_concurrente(indice, tabla, constraint)
        # ACCESS EXCLUSIVE de milisegundos por tabla, cada una en su propia transacción; sin
        # esperar detrás de una transacción larga.
        op.execute("SET lock_timeout = '5s'")
        for tabla, (indice, constraint) in _CLAVES.items():
            op.execute(
                f"""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                                    WHERE conname = '{constraint}' AND conrelid = 'public.{tabla}'::regclass) THEN
                        ALTER TABLE {tabla} ADD CONSTRAINT {constraint} UNIQUE USING INDEX {indice};
                    END IF;
                END $$;
                """
            )
        op.execute("RESET lock_timeout")
    op.execute(_PREVENT_MUTATION)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS prevent_mutation()")
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        # Soltar la constraint borra también su índice. Una tabla por transacción.
        for tabla, (_indice, constraint) in _CLAVES.items():
            op.execute(f"ALTER TABLE {tabla} DROP CONSTRAINT IF EXISTS {constraint}")
        op.execute("RESET lock_timeout")
