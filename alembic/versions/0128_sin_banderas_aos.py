"""Salen `tenants.aos_procedures` y `tenants.aos_routing` (ADR-0191, HU-AOS-69): el motor de
procedimientos y el ruteo entre especialistas ya no son banderas por negocio.

Segunda fase del retiro (regla de dos fases de `/db-migration`). La primera ya está desplegada: el
código no las lee ni las escribe (decisión del dueño, 2026-11-13: el motor siempre encendido y el ruteo
siempre en `active`). Pasos, en este orden (el trigger depende de las columnas por su `UPDATE OF`;
las funciones las nombran en su cuerpo):

1. `DROP TRIGGER tenants_aos_banderas_guard` y su función (0097, recreados en 0126 y 0127).
2. `DROP FUNCTION aos_fijar_banderas(uuid, boolean, text, uuid, text)` (la firma de 0127): ya no
   hay banderas que fijar ni quién la llame.
3. `DROP CONSTRAINT ck_tenants_aos_routing` (0094), el único CHECK que quedó tras 0127.
4. `ALTER TABLE tenants DROP COLUMN aos_procedures, DROP COLUMN aos_routing`: solo catálogo, sin
   reescribir la tabla.

Aviso (no bloquea): si queda algún negocio vivo con una bandera distinta de `(true, 'active')`, la
migración lo dice con un WARNING antes de borrar: esos negocios ya corren con todo encendido desde
el despliegue de la primera fase, y el dato no se pierde en silencio. No aborta (lección de 0127: una
base nueva trae `tenant-1` de 0003 con los valores por defecto). Las filas `agent_os.banderas` de
`audit_log` se quedan como historia. `ACCESS EXCLUSIVE` breve sobre `tenants` (DROP TRIGGER,
DROP CONSTRAINT, DROP COLUMN), con `SET LOCAL lock_timeout` de 5 s; `tenants` es chica.

El downgrade repone las columnas con la conducta de hoy para los negocios existentes (`true` y
`'active'`, sin reescritura desde PG 11) y los valores por defecto de siempre para los nuevos
(`false` y `'off'`, los que exige el guardia al nacer); su CHECK; y el texto de 0127 de la función
y del guardia (congelado aquí). Re-ejecutable.

Revision ID: 0128
Revises: 0127
Create Date: 2026-11-13
"""

from __future__ import annotations

from alembic import op

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None

_PRECHECK = """
DO $$
DECLARE
    vivos integer;
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'tenants' AND column_name = 'aos_procedures')
       AND EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'tenants' AND column_name = 'aos_routing') THEN
        -- Dinámico: si las columnas ya no existen (re-ejecución), el SQL estático no compilaría.
        EXECUTE 'SELECT count(*) FROM public.tenants WHERE deleted_at IS NULL '
                'AND (aos_procedures IS DISTINCT FROM true OR aos_routing IS DISTINCT FROM ''active'')'
           INTO vivos;
        IF vivos > 0 THEN
            RAISE WARNING '0128: se borran aos_procedures y aos_routing con % negocio(s) vivo(s) que no tenían '
                          'todo encendido (ya corren con el motor y el ruteo activos)', vivos;
        END IF;
    END IF;
END $$;
"""

# Downgrade: el texto de 0127 de la función y del guardia.
_FUNCION_0127 = """
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

_GUARDIA_0127 = """
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


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_PRECHECK)
    op.execute("DROP TRIGGER IF EXISTS tenants_aos_banderas_guard ON public.tenants")
    op.execute("DROP FUNCTION IF EXISTS public.tenants_aos_banderas_guard()")
    op.execute("DROP FUNCTION IF EXISTS public.aos_fijar_banderas(uuid, boolean, text, uuid, text)")
    op.execute(
        "ALTER TABLE public.tenants DROP CONSTRAINT IF EXISTS ck_tenants_aos_routing, "
        "DROP COLUMN IF EXISTS aos_procedures, DROP COLUMN IF EXISTS aos_routing"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Los negocios existentes vuelven con la conducta de hoy (todo encendido); los nuevos nacen con
    # los valores por defecto que exige el guardia.
    op.execute(
        "ALTER TABLE public.tenants ADD COLUMN IF NOT EXISTS aos_procedures boolean NOT NULL DEFAULT true, "
        "ADD COLUMN IF NOT EXISTS aos_routing text NOT NULL DEFAULT 'active'"
    )
    op.execute(
        "ALTER TABLE public.tenants ALTER COLUMN aos_procedures SET DEFAULT false, "
        "ALTER COLUMN aos_routing SET DEFAULT 'off'"
    )
    op.execute(
        """
DO $$
BEGIN
IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tenants_aos_routing'
               AND conrelid = 'public.tenants'::regclass) THEN
  ALTER TABLE public.tenants ADD CONSTRAINT ck_tenants_aos_routing CHECK (aos_routing IN ('off','shadow','active'));
END IF;
END $$;
"""
    )
    op.execute(_FUNCION_0127)
    op.execute(_GUARDIA_0127)
