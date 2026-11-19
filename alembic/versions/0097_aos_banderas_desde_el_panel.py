"""Banderas del Agent OS desde el panel del master (HU-AOS-15, criterio «Orden de banderas»).

1. `aos_fijar_banderas(p_tenant, p_runtime, p_procedures, p_routing, p_admin_id, p_actor)`: el
   único escritor de `tenants.aos_runtime`, `aos_procedures` y `aos_routing`. `SECURITY DEFINER`
   (corre con el dueño de `tenants`), `search_path` fijo y nombres calificados, `REVOKE ALL FROM
   PUBLIC` y `EXECUTE` solo para `app_runtime` (patrón de 0072). Quién puede llamarla:
   - con `p_admin_id`: ese admin debe existir, estar activo y ser `master` (la API del panel con
     el rol de la aplicación: el request del master ya no abre un engine con el rol dueño). El
     tester no, tampoco en test (decisión del dueño, 2026-10-20). Ojo: desde `app_runtime` cualquiera podría pasar el id de un
     master, así que esto NO autentica: protege la integridad de la auditoría (el actor sale de
     `admins`, no del llamador). La autorización real es la de la API. Además, con admin se
     rechaza la llamada bajo el `tenant_scope` de un negocio (`app.tenant_id` fijado);
   - sin `p_admin_id`: solo una sesión del dueño de `tenants` o de un superusuario (el CLI
     `migrar-a-v3` / `ruteo` y «Preparar mis datos», que ya escribían con el rol dueño).
   Valida el orden (`aos_procedures` y `aos_routing` ≠ `off` exigen `v3`; los CHECK de 0070 y 0094
   quedan de respaldo) con SQLSTATE `check_violation`, bloquea la fila (`FOR UPDATE`) y, si algo
   cambia, escribe el UPDATE y una fila de `audit_log` (`agent_os.banderas`, antes y después, con el
   actor) en la misma transacción: el cambio nunca queda sin rastro. Sin cambios no escribe nada.
   `AOS_CONNECTION_ROUTING` es una variable de entorno que la base no ve: la valida quien llama.
2. Trigger `tenants_aos_banderas_guard` (`BEFORE UPDATE OF aos_runtime, aos_procedures, aos_routing`,
   y `BEFORE INSERT`): rechaza el cambio de una bandera (o un negocio que nazca sin los defaults
   `legacy`/`false`/`off`) si quien escribe (`current_user`) no es miembro del dueño de `tenants`
   ni superusuario. Dentro de la función `current_user` es el dueño, así que pasa; un
   UPDATE directo de `app_runtime` cae. Es la evaluación de «quitar `UPDATE (aos_*)` a
   `app_runtime`» que pedía HU-AOS-02: `app_runtime` tiene `UPDATE` sobre toda la tabla (0003) y un
   `REVOKE` por columna no le resta nada; acotar el GRANT por columnas haría nacer sin permiso a
   cada columna nueva de `tenants`. El trigger cubre las tres banderas sin tocar los GRANT.

El INSERT en `audit_log` (RLS forzada) lo hace el dueño, que ya tiene `BYPASSRLS` (lo exigen 0034
y 0052; runbook de producción, 4.3) o es superusuario (CI, local y test). Sin eso la función falla
entera (nada se escribe), como `purge_tenant`. Excepción conocida al escritor único: los journeys
de evals (`evals/journeys.py`) y algunos tests escriben las banderas directo con el rol dueño, que el
trigger permite.

Solo crea una función y un trigger: sin reescritura ni bloqueos largos (`lock_timeout` de 5 s
para el `CREATE TRIGGER`, que toma un SHARE ROW EXCLUSIVE breve sobre `tenants`). Re-ejecutable.

Revision ID: 0097
Revises: 0096
Create Date: 2026-10-20
"""

from __future__ import annotations

from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None

_FUNCION = """
CREATE OR REPLACE FUNCTION public.aos_fijar_banderas(
    p_tenant uuid,
    p_runtime text,
    p_procedures boolean,
    p_routing text,
    p_admin_id uuid,
    p_actor text
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    antes record;
    v_actor text;
BEGIN
    IF p_admin_id IS NOT NULL THEN
        -- El master opera la plataforma, no un negocio: su llamada nunca va bajo el tenant_scope
        -- de un negocio (defensa en profundidad frente a un camino de negocio que la llame).
        IF NULLIF(pg_catalog.current_setting('app.tenant_id', true), '') IS NOT NULL THEN
            RAISE EXCEPTION 'aos_fijar_banderas: con admin, fuera del tenant_scope de un negocio'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
        SELECT 'admin:' || email INTO v_actor
          FROM public.admins WHERE id = p_admin_id AND role = 'master' AND active;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'aos_fijar_banderas: solo el admin master cambia las banderas del Agent OS'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    ELSIF NOT (
        pg_catalog.pg_has_role(
            session_user,
            (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid = 'public.tenants'::regclass),
            'MEMBER'
        )
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = session_user AND rolsuper)
    ) THEN
        RAISE EXCEPTION 'aos_fijar_banderas: sin admin master, solo el rol dueño cambia las banderas'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    IF p_runtime IS NULL OR p_runtime NOT IN ('legacy', 'v3')
       OR p_procedures IS NULL
       OR p_routing IS NULL OR p_routing NOT IN ('off', 'shadow', 'active') THEN
        RAISE EXCEPTION 'aos_fijar_banderas: valores inválidos' USING ERRCODE = 'invalid_parameter_value';
    END IF;
    -- Orden de ADR-0112 (y ADR-0138): procedimientos y enrutamiento solo sobre v3.
    IF p_procedures AND p_runtime <> 'v3' THEN
        RAISE EXCEPTION 'aos_fijar_banderas: aos_procedures exige aos_runtime = v3'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_tenants_aos_order';
    END IF;
    IF p_routing <> 'off' AND p_runtime <> 'v3' THEN
        RAISE EXCEPTION 'aos_fijar_banderas: aos_routing exige aos_runtime = v3'
            USING ERRCODE = 'check_violation', CONSTRAINT = 'ck_tenants_aos_routing_order';
    END IF;

    SELECT aos_runtime, aos_procedures, aos_routing INTO antes
      FROM public.tenants WHERE id = p_tenant AND deleted_at IS NULL FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'aos_fijar_banderas: el negocio % no existe', p_tenant USING ERRCODE = 'no_data_found';
    END IF;
    IF (antes.aos_runtime, antes.aos_procedures, antes.aos_routing) = (p_runtime, p_procedures, p_routing) THEN
        RETURN jsonb_build_object('cambio', false);
    END IF;

    UPDATE public.tenants
       SET aos_runtime = p_runtime, aos_procedures = p_procedures, aos_routing = p_routing
     WHERE id = p_tenant;

    IF p_admin_id IS NULL THEN
        v_actor := COALESCE(NULLIF(p_actor, ''), 'system:' || session_user);
    END IF;
    INSERT INTO public.audit_log (id, tenant_id, actor, action, input, output, created_at)
    VALUES (
        gen_random_uuid(), p_tenant, left(v_actor, 200), 'agent_os.banderas',
        jsonb_build_object('runtime', antes.aos_runtime, 'procedures', antes.aos_procedures,
                           'routing', antes.aos_routing, 'admin_id', p_admin_id),
        jsonb_build_object('runtime', p_runtime, 'procedures', p_procedures, 'routing', p_routing),
        now()
    );
    RETURN jsonb_build_object('cambio', true);
END;
$$;

REVOKE ALL ON FUNCTION public.aos_fijar_banderas(uuid, text, boolean, text, uuid, text) FROM PUBLIC;
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        GRANT EXECUTE ON FUNCTION public.aos_fijar_banderas(uuid, text, boolean, text, uuid, text) TO app_runtime;
    END IF;
END $do$;
"""

_GUARDIA = """
CREATE OR REPLACE FUNCTION public.tenants_aos_banderas_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    cambia boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        cambia := (NEW.aos_runtime, NEW.aos_procedures, NEW.aos_routing) IS DISTINCT FROM ('legacy', false, 'off');
    ELSE
        cambia := (NEW.aos_runtime, NEW.aos_procedures, NEW.aos_routing)
                  IS DISTINCT FROM (OLD.aos_runtime, OLD.aos_procedures, OLD.aos_routing);
    END IF;
    IF cambia
       AND NOT pg_catalog.pg_has_role(
           current_user, (SELECT c.relowner FROM pg_catalog.pg_class c WHERE c.oid = TG_RELID), 'MEMBER')
       AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = current_user AND rolsuper) THEN
        RAISE EXCEPTION 'las banderas del Agent OS solo se cambian con aos_fijar_banderas (master)'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tenants_aos_banderas_guard ON public.tenants;
CREATE TRIGGER tenants_aos_banderas_guard
    BEFORE INSERT OR UPDATE OF aos_runtime, aos_procedures, aos_routing ON public.tenants
    FOR EACH ROW EXECUTE FUNCTION public.tenants_aos_banderas_guard();
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(_FUNCION)
    op.execute(_GUARDIA)
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("DROP TRIGGER IF EXISTS tenants_aos_banderas_guard ON public.tenants")
    op.execute("DROP FUNCTION IF EXISTS public.tenants_aos_banderas_guard()")
    op.execute("DROP FUNCTION IF EXISTS public.aos_fijar_banderas(uuid, text, boolean, text, uuid, text)")
    op.execute("RESET lock_timeout")
