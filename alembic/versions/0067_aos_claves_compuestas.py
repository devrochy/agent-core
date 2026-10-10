"""Agent OS · expand 1/5: claves `(tenant_id, id)` y `prevent_mutation()` (HU-AOS-02, ADR-0114/0116).

- `UNIQUE (tenant_id, id)` en `channel_connections`, `conversations`, `customers` y
  `meta_connections`: destino de las FKs compuestas del Agent OS (una referencia entre negocios
  es imposible a nivel de esquema, ADR-0116 §4). Son tablas grandes: el índice se crea con
  `CREATE UNIQUE INDEX CONCURRENTLY` fuera de la transacción y después se ata como constraint
  con `ADD CONSTRAINT … USING INDEX` (lock breve, sin escaneo).
- `prevent_mutation()`: función genérica nueva (ADR-0114 §1). Rechaza DELETE y todo UPDATE salvo
  las columnas que recibe como argumento del trigger (`TG_ARGV`). Sin argumentos, la tabla es
  append-only estricta. `purge_tenant` (0011) la salta al desactivar los triggers.

Sin cambio de conducta: nada en runtime usa la función ni las constraints nuevas (el `id` ya es
único, así que `(tenant_id, id)` también lo es).

Los índices concurrentes van PRIMERO: si la parte transaccional falla, la migración se puede
volver a correr (un índice válido se salta; uno INVALID de un intento anterior se rehace).

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
}

_PREVENT_MUTATION = """
CREATE OR REPLACE FUNCTION prevent_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_old jsonb;
    v_new jsonb;
    v_col text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '% es append-only (DELETE)', TG_TABLE_NAME;
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


def _indice_unico_concurrente(indice: str, tabla: str) -> None:
    """Crea el índice sin bloquear escrituras; idempotente ante un intento fallido."""
    conn = op.get_bind()
    valido = conn.exec_driver_sql(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = %(indice)s",
        {"indice": indice},
    ).scalar()
    if valido:
        return
    if valido is False:  # quedó INVALID de un CONCURRENTLY interrumpido
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {indice}")
    op.execute(f"CREATE UNIQUE INDEX CONCURRENTLY {indice} ON {tabla} (tenant_id, id)")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for tabla, (indice, _constraint) in _CLAVES.items():
            _indice_unico_concurrente(indice, tabla)

    # ACCESS EXCLUSIVE de milisegundos por tabla; sin esperar detrás de una transacción larga.
    op.execute("SET LOCAL lock_timeout = '5s'")
    for tabla, (indice, constraint) in _CLAVES.items():
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{constraint}') THEN
                    ALTER TABLE {tabla} ADD CONSTRAINT {constraint} UNIQUE USING INDEX {indice};
                END IF;
            END $$;
            """
        )
    op.execute(_PREVENT_MUTATION)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP FUNCTION IF EXISTS prevent_mutation()")
    # Soltar la constraint borra también su índice.
    for tabla, (_indice, constraint) in _CLAVES.items():
        op.execute(f"ALTER TABLE {tabla} DROP CONSTRAINT IF EXISTS {constraint}")
