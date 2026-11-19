"""Sale `tenants.aos_runtime` (ADR-0191, HU-AOS-69): todo negocio corre en el Agent OS.

Segunda fase del retiro de la columna. La primera ya está desplegada: desde 0126 todo negocio nace
en `'v3'` y el escritor no acepta otro valor, y desde el PR anterior el código no la lee. Pasos, en
este orden (el trigger depende de la columna por su `UPDATE OF`; la función la nombra en su cuerpo):

1. Quita los CHECK que la usan: `ck_tenants_aos_runtime` y `ck_tenants_aos_order` (0070) y
   `ck_tenants_aos_routing_order` (0094). Con un solo runtime, el orden de ADR-0112 y ADR-0138 no
   tiene nada que exigir; `ck_tenants_aos_routing` (valores de `aos_routing`) se queda.
2. `aos_fijar_banderas` pierde `p_runtime`: `DROP FUNCTION` de la firma de 6 argumentos (un
   `CREATE OR REPLACE` con otra aridad dejaría dos sobrecargas) y `CREATE` de la de 5, con el
   mismo `REVOKE`/`GRANT` a `app_runtime`. La auditoría (`audit_log`, `agent_os.banderas`) deja de
   llevar `runtime` en `input` y `output`.
3. El guardia `tenants_aos_banderas_guard` mira solo `aos_procedures` y `aos_routing` (un negocio
   nace con `(false, 'off')`) y se recrea el trigger sin la columna en su `UPDATE OF`.
4. `ALTER TABLE tenants DROP COLUMN aos_runtime`: solo catálogo, sin reescribir la tabla.

Aviso (no bloquea): si queda algún negocio con `aos_runtime` distinto de `'v3'`, la migración lo
dice con un WARNING (cuántos vivos y cuántos dados de baja) antes de borrar la columna: el dato no
se pierde en silencio. No aborta porque una base nueva trae `tenant-1` (0003) en `'legacy'`, el
valor por defecto de 0070. Que ningún negocio vivo de test o producción esté fuera de `'v3'` se
comprueba con una consulta antes de desplegar (ver el PR). La función
nueva la crea el rol que migra (el mismo de 0097: dueño de `tenants`, con BYPASSRLS). `ACCESS EXCLUSIVE` breve sobre
`tenants` (DROP CONSTRAINT, DROP COLUMN, trigger), con `SET LOCAL lock_timeout` de 5 s; `tenants`
es chica. Despliegue: esta migración y el código que llama a la función de 5 argumentos van juntos;
las tres apps (API, worker y beat) con la misma imagen.

El downgrade repone la columna en `'v3'` (`NOT NULL DEFAULT 'v3'`, sin reescritura desde PG 11),
sus tres CHECK y el texto de 0126 de la función y del guardia (congelado aquí). Re-ejecutable.

Revision ID: 0127
Revises: 0126
Create Date: 2026-11-13
"""

from __future__ import annotations

from alembic import op

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None

_FUNCION = """
CREATE OR REPLACE FUNCTION public.aos_fijar_banderas(
    p_tenant uuid,
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

    IF p_procedures IS NULL
       OR p_routing IS NULL OR p_routing NOT IN ('off', 'shadow', 'active') THEN
        RAISE EXCEPTION 'aos_fijar_banderas: valores inválidos' USING ERRCODE = 'invalid_parameter_value';
    END IF;

    SELECT aos_procedures, aos_routing INTO antes
      FROM public.tenants WHERE id = p_tenant AND deleted_at IS NULL FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'aos_fijar_banderas: el negocio % no existe', p_tenant USING ERRCODE = 'no_data_found';
    END IF;
    IF (antes.aos_procedures, antes.aos_routing) = (p_procedures, p_routing) THEN
        RETURN jsonb_build_object('cambio', false);
    END IF;

    UPDATE public.tenants
       SET aos_procedures = p_procedures, aos_routing = p_routing
     WHERE id = p_tenant;

    IF p_admin_id IS NULL THEN
        v_actor := COALESCE(NULLIF(p_actor, ''), 'system:' || session_user);
    END IF;
    INSERT INTO public.audit_log (id, tenant_id, actor, action, input, output, created_at)
    VALUES (
        gen_random_uuid(), p_tenant, left(v_actor, 200), 'agent_os.banderas',
        jsonb_build_object('procedures', antes.aos_procedures, 'routing', antes.aos_routing,
                           'admin_id', p_admin_id),
        jsonb_build_object('procedures', p_procedures, 'routing', p_routing),
        now()
    );
    RETURN jsonb_build_object('cambio', true);
END;
$$;

REVOKE ALL ON FUNCTION public.aos_fijar_banderas(uuid, boolean, text, uuid, text) FROM PUBLIC;
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        GRANT EXECUTE ON FUNCTION public.aos_fijar_banderas(uuid, boolean, text, uuid, text) TO app_runtime;
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
        cambia := (NEW.aos_procedures, NEW.aos_routing) IS DISTINCT FROM (false, 'off');
    ELSE
        cambia := (NEW.aos_procedures, NEW.aos_routing) IS DISTINCT FROM (OLD.aos_procedures, OLD.aos_routing);
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
    BEFORE INSERT OR UPDATE OF aos_procedures, aos_routing ON public.tenants
    FOR EACH ROW EXECUTE FUNCTION public.tenants_aos_banderas_guard();
"""

# Downgrade: los CHECK de 0070 y 0094, y el texto de 0126 de la función y del guardia.
_CHECKS_0070_0094 = """
DO $$
BEGIN
IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tenants_aos_runtime'
               AND conrelid = 'public.tenants'::regclass) THEN
  ALTER TABLE public.tenants ADD CONSTRAINT ck_tenants_aos_runtime CHECK (aos_runtime IN ('legacy','v3'));
END IF;
IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tenants_aos_order'
               AND conrelid = 'public.tenants'::regclass) THEN
  ALTER TABLE public.tenants ADD CONSTRAINT ck_tenants_aos_order CHECK (NOT aos_procedures OR aos_runtime = 'v3');
END IF;
IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tenants_aos_routing_order'
               AND conrelid = 'public.tenants'::regclass) THEN
  ALTER TABLE public.tenants ADD CONSTRAINT ck_tenants_aos_routing_order
    CHECK (aos_routing = 'off' OR aos_runtime = 'v3');
END IF;
END $$;
"""

_FUNCION_0126 = """
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

_GUARDIA_0126 = """
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


_PRECHECK = """
DO $$
DECLARE
    vivos integer;
    de_baja integer;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'tenants' AND column_name = 'aos_runtime') THEN
        -- Dinámico: si la columna ya no existe (re-ejecución), el SQL estático no compilaría.
        EXECUTE 'SELECT count(*) FILTER (WHERE deleted_at IS NULL), count(*) FILTER (WHERE deleted_at IS NOT NULL) '
                'FROM public.tenants WHERE aos_runtime IS DISTINCT FROM ''v3'''
           INTO vivos, de_baja;
        IF vivos + de_baja > 0 THEN
            RAISE WARNING '0127: se borra aos_runtime con % negocio(s) vivo(s) y % dado(s) de baja fuera de v3',
                vivos, de_baja;
        END IF;
    END IF;
END $$;
"""


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_PRECHECK)
    op.execute(
        "ALTER TABLE public.tenants DROP CONSTRAINT IF EXISTS ck_tenants_aos_runtime, "
        "DROP CONSTRAINT IF EXISTS ck_tenants_aos_order, DROP CONSTRAINT IF EXISTS ck_tenants_aos_routing_order"
    )
    op.execute("DROP FUNCTION IF EXISTS public.aos_fijar_banderas(uuid, text, boolean, text, uuid, text)")
    op.execute(_FUNCION)
    op.execute(_GUARDIA)
    op.execute("ALTER TABLE public.tenants DROP COLUMN IF EXISTS aos_runtime")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS aos_runtime text NOT NULL DEFAULT 'v3'")
    op.execute(_CHECKS_0070_0094)
    op.execute("DROP FUNCTION IF EXISTS public.aos_fijar_banderas(uuid, boolean, text, uuid, text)")
    op.execute(_FUNCION_0126)
    op.execute(_GUARDIA_0126)
