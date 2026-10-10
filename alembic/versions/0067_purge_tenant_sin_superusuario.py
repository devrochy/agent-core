"""`purge_tenant` sin superusuario: triggers apagados con `session_replication_role`.

La `0011` apagaba los triggers con `ALTER TABLE … DISABLE TRIGGER ALL`. Eso incluye los
triggers internos de las FKs, y PostgreSQL solo deja tocarlos a un superusuario
("permission denied: … is a system trigger"). En producción el dueño de las tablas es
`agent_core_migrations NOSUPERUSER BYPASSRLS` (`docs/runbooks/produccion-vps.md`, 4.3), así
que la purga fallaba siempre y hacía rollback completo: sin fuga, pero sin borrado. En CI,
local y test no se veía porque ahí las migraciones corren como superusuario.

Ahora la función se declara con `SET session_replication_role = replica`: mientras borra los
datos del negocio, no disparan los triggers de modo `origin` (los de las FKs y los de usuario,
como los inmutables de `audit_log` y `usage_ledger`), así que el orden de borrado y los ciclos
de FK no importan. Al ser una cláusula `SET` de la función, PostgreSQL restaura el valor
anterior al salir, por éxito o por error: no se filtra al resto de la transacción del llamador
(un `SET LOCAL` en el cuerpo sin esa cláusula duraría hasta el fin de la transacción). Un
trigger `ENABLE ALWAYS` sí dispararía; hoy no hay ninguno.

El borrado final de la fila `tenants` vuelve a `origin` (`set_config` local, acotado a la
función por la cláusula `SET`): las FKs que apuntan al negocio desde fuera de él, como
`provisioning_requests.negocio_tenant_id` del vendedor (`ON DELETE SET NULL`, `0059`), se
aplican, y una FK desconocida hacia `tenants` aborta la purga en lugar de quedar rota.

Otros cambios respecto de la `0011`:
- Solo purga un negocio **suspendido** (`tenants.status = 'suspended'`, el estado del ciclo de
  vida que pone `tenant deactivate`; no la falta de pago de ADR-0045). Si está activo o no
  existe, falla sin borrar nada. Es defensa en profundidad: `core/tenants.py` ya lo exige antes
  de llamar, pero una llamada directa (o inyectada) no puede borrar un negocio en operación.
- Descubre las tablas con `tenant_id` en todos los esquemas propios, no solo en `public`:
  `meta_private.meta_tokens` (tokens cifrados de Meta) quedaba huérfana.
- `search_path` fijado a `pg_catalog, public, pg_temp`, nombres calificados y descubrimiento por
  `pg_catalog` (solo tablas, nunca vistas; las particiones se borran desde la tabla padre).
- `EXECUTE` solo para `app_runtime` (el que la llama en `core/tenants.py`), no para PUBLIC:
  desde ahora la función funciona en producción.

`session_replication_role` es de nivel superusuario. Desde PostgreSQL 15 se concede con
`GRANT SET ON PARAMETER session_replication_role TO <dueño>`, y ese GRANT lo hace el
superusuario (el dueño no puede concedérselo a sí mismo): está en el paso 4.3 del runbook de
producción. Sin superusuario ni ese permiso (en el rol que migra y en el dueño de la función),
la migración aborta a propósito, como la `0052`.

Se mantienen el resto de garantías de la `0011`: SECURITY DEFINER (el dueño omite RLS con BYPASSRLS),
transaccional (si algo falla, rollback completo) y master-only con frase `ELIMINAR <slug>` en
`core/tenants.py` (CLI, panel y API). Solo reemplaza una función: sin bloqueos sobre tablas.

Revision ID: 0067
Revises: 0066
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None

# Roles que necesitan el permiso: el que migra (CREATE FUNCTION valida la cláusula SET) y el
# dueño de la función (con SECURITY DEFINER, la cláusula se aplica con su identidad). Si la
# función no existe todavía, solo cuenta el que migra. `has_parameter_privilege` es de PG 15+.
_ROLES_SIN_PERMISO = """
    select r.rolname
    from pg_roles r
    where (r.rolname = current_user
           or r.oid = (select p.proowner from pg_proc p
                       where p.oid = to_regprocedure('public.purge_tenant(uuid)')))
      and not r.rolsuper
      and not has_parameter_privilege(r.oid, 'session_replication_role', 'SET')
"""

_PURGE_TENANT = """
CREATE OR REPLACE FUNCTION purge_tenant(p_tenant_id uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
SET session_replication_role = replica
AS $$
DECLARE
    rec record;
BEGIN
    -- Solo un negocio suspendido (ciclo de vida, tenants.status) se purga. FOR UPDATE: una
    -- reactivación concurrente espera a que la purga termine o falle.
    PERFORM 1 FROM public.tenants
    WHERE id = p_tenant_id AND status = 'suspended'
    FOR UPDATE;
    IF NOT FOUND THEN
        IF EXISTS (SELECT 1 FROM public.tenants WHERE id = p_tenant_id) THEN
            RAISE EXCEPTION 'purge_tenant: el negocio % no está suspendido', p_tenant_id
                USING ERRCODE = 'object_not_in_prerequisite_state',
                      HINT = 'Suspéndelo primero (agent-core tenant deactivate <slug>).';
        END IF;
        RAISE EXCEPTION 'purge_tenant: el negocio % no existe', p_tenant_id
            USING ERRCODE = 'no_data_found';
    END IF;

    -- Toda tabla con tenant_id (core + packs + meta_private), sin triggers de FK ni de
    -- inmutabilidad: el orden no importa. Las particiones se borran desde su tabla padre.
    FOR rec IN
        SELECT n.nspname, c.relname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND n.nspname NOT LIKE 'pg\\_%'
          AND c.relkind IN ('r', 'p')
          AND NOT c.relispartition
          AND a.attname = 'tenant_id'
          AND NOT a.attisdropped
        ORDER BY n.nspname, c.relname
    LOOP
        EXECUTE format('DELETE FROM %I.%I WHERE tenant_id = $1', rec.nspname, rec.relname)
            USING p_tenant_id;
    END LOOP;

    -- La fila tenants, con las FKs encendidas: lo que apunta al negocio desde otro negocio
    -- (provisioning_requests.negocio_tenant_id, ON DELETE SET NULL) se aplica, y una FK
    -- inesperada aborta. Local: la cláusula SET de la función lo acota a la función.
    PERFORM pg_catalog.set_config('session_replication_role', 'origin', true);
    DELETE FROM public.tenants WHERE id = p_tenant_id;
END;
$$;

-- Solo el runtime la llama (core/tenants.py); el dueño conserva EXECUTE por ser owner.
REVOKE ALL ON FUNCTION purge_tenant(uuid) FROM PUBLIC;
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        GRANT EXECUTE ON FUNCTION purge_tenant(uuid) TO app_runtime;
    END IF;
END $do$;
"""

# Cuerpo y permisos de la 0011, para el downgrade.
_PURGE_TENANT_0011 = """
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
    FOR rec IN
        SELECT table_name
        FROM information_schema.columns
        WHERE column_name = 'tenant_id' AND table_schema = 'public'
    LOOP
        tbl := quote_ident(rec.table_name);
        EXECUTE format('ALTER TABLE %s DISABLE TRIGGER ALL', tbl);
        EXECUTE format('DELETE FROM %s WHERE tenant_id = $1', tbl) USING p_tenant_id;
        EXECUTE format('ALTER TABLE %s ENABLE TRIGGER ALL', tbl);
    END LOOP;

    DELETE FROM tenant_user WHERE tenant_id = p_tenant_id;
    DELETE FROM tenants WHERE id = p_tenant_id;
END;
$$;

GRANT EXECUTE ON FUNCTION purge_tenant(uuid) TO PUBLIC;
"""


def upgrade() -> None:
    bind = op.get_bind()
    sin_permiso = bind.execute(sa.text(_ROLES_SIN_PERMISO)).scalars().all()
    if sin_permiso:
        raise RuntimeError(
            f"0067: {', '.join(sin_permiso)} no es superusuario ni puede fijar "
            "session_replication_role, y purge_tenant lo necesita. Como superusuario: "
            "GRANT SET ON PARAMETER session_replication_role TO <rol>; "
            "(docs/runbooks/produccion-vps.md, paso 4.3) y vuelve a migrar."
        )
    # CREATE OR REPLACE conserva el dueño de la función.
    op.execute(_PURGE_TENANT)


def downgrade() -> None:
    # Vuelve al cuerpo y los permisos de la 0011 (CREATE OR REPLACE también reemplaza las
    # cláusulas SET). Con un dueño sin superusuario, la purga vuelve a fallar.
    op.execute(_PURGE_TENANT_0011)
