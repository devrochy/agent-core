"""Los negocios nacen en `aos_runtime = 'v3'` y el escritor de banderas ya no acepta `legacy` (ADR-0191, HU-AOS-69).

1. `tenants.aos_runtime` cambia su valor por defecto de `'legacy'` a `'v3'`: un negocio nuevo nace en el
   Agent OS sin el paso «nacer en legacy y migrar» del alta.
2. El trigger `tenants_aos_banderas_guard` (0097) exige que un negocio nazca con `('v3', false, 'off')`
   en vez de `('legacy', false, 'off')` cuando quien escribe no es el dueño de `tenants`.
3. `aos_fijar_banderas` (0097), el único escritor de las banderas, rechaza `p_runtime = 'legacy'`
   (`invalid_parameter_value`): entre esta migración y la que suelta la columna nadie puede dejar un
   negocio en un runtime que ya no existe en el código.

No toca datos: que no queden negocios en `legacy` se comprueba con un comando antes de desplegar, no
con un precheck aquí. El CHECK `ck_tenants_aos_runtime` (0070) sigue aceptando `legacy` hasta que se
suelte la columna (otra migración, después de desplegar esta). Las funciones llevan su texto congelado aquí (el de 0097
con solo esas dos condiciones cambiadas); el downgrade reinstala el texto de 0097, también congelado.
El guardia recrea el trigger (`DROP`/`CREATE TRIGGER`): un SHARE ROW EXCLUSIVE breve sobre `tenants`. Sin reescritura de tabla: `SET DEFAULT` y
`CREATE OR REPLACE FUNCTION`, con `SET LOCAL lock_timeout` de 5 s. Re-ejecutable; el downgrade vuelve al texto de
0097 y al valor por defecto `'legacy'`.

Revision ID: 0126
Revises: 0125
Create Date: 2026-11-12
"""

from __future__ import annotations

from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None

_FUNCION_V3 = """
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

    IF p_runtime IS NULL OR p_runtime <> 'v3'
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

_GUARDIA_V3 = """
CREATE OR REPLACE FUNCTION public.tenants_aos_banderas_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp
AS $$
DECLARE
    cambia boolean;
BEGIN
    IF TG_OP = 'INSERT' THEN
        cambia := (NEW.aos_runtime, NEW.aos_procedures, NEW.aos_routing) IS DISTINCT FROM ('v3', false, 'off');
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

# Texto de 0097, para el downgrade.
_FUNCION_0097 = """
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

_GUARDIA_0097 = """
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
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE public.tenants ALTER COLUMN aos_runtime SET DEFAULT 'v3'")
    op.execute(_FUNCION_V3)
    op.execute(_GUARDIA_V3)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_FUNCION_0097)
    op.execute(_GUARDIA_0097)
    op.execute("ALTER TABLE public.tenants ALTER COLUMN aos_runtime SET DEFAULT 'legacy'")
