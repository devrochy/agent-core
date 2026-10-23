"""Una Página de Facebook o una cuenta de Instagram pertenecen a un solo negocio (ADR-0152).

El webhook rutea cada evento de Messenger e Instagram por el `page_id` o el `ig_user_id`
(`webhook_route_tenant.py`). Hasta ahora `resolve_tenant_for_page` y
`resolve_tenant_for_ig_user` (0021) devolvían el primero que encontraran (`LIMIT 1` sin
orden) y contaban también los activos de conexiones borradas: si dos negocios guardaran la
misma Página, los mensajes irían a uno cualquiera, y quien registrara la Página de otro
recibiría sus conversaciones. Es el mismo defecto que ADR-0095 corrigió para la WABA y el
número (0052).

Hoy nada escribe en `fb_pages` ni en `ig_accounts` (la salida de Messenger e Instagram se
está portando, HU-CHAN-01 y HU-CHAN-04), así que es un defecto latente: esta migración lo
cierra ANTES de que existan altas.

Desde aquí las dos funciones de ruteo miran solo conexiones vivas (no borradas y fuera de
`draft`), cuentan los negocios que reclaman el activo y devuelven NULL si hay cero o más de
uno: un evento ambiguo queda como `unresolved_tenant` o `tenant_conflict` en
`webhook_events` en vez de ir a un negocio cualquiera.
`meta_messaging_asset_claimed_by_other_tenant` responde a las altas si otro negocio ya
tiene la Página o la cuenta; devuelve un booleano y nunca dice cuál (mismo criterio que
ADR-0039). `p_tenant` puede ser NULL: se compara con `IS DISTINCT FROM`, así cualquier
dueño cuenta como "otro".

RLS: `fb_pages`, `ig_accounts` y `meta_connections` tienen RLS forzada. Una función
SECURITY DEFINER cuyo dueño no sea superusuario ni tenga `BYPASSRLS` ve 0 filas: el ruteo
daría siempre NULL y el reclamo siempre "libre". La migración aborta si el rol que la corre
no puede saltar RLS (ADR-0039, Consecuencias), y con ese mismo rol lista en el log los
activos que hoy reclaman dos negocios: dejan de rutear hasta que se borre la conexión que
sobra.

Las tres funciones fijan `search_path = public, pg_temp` y solo las ejecuta `app_runtime`:
los dos resolvedores conservaban EXECUTE para PUBLIC desde 0021.

Revision ID: 0104
Revises: 0103
Create Date: 2026-10-23
"""
from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"

# Activos que hoy reclaman dos o más negocios con conexiones vivas: con la regla nueva
# dejan de rutear (NULL). Se listan para corregirlos a mano.
_PAGINAS_DUPLICADAS = """
    select f.page_id, array_agg(distinct f.tenant_id::text) as tenants
    from fb_pages f
    join meta_connections c on c.id = f.connection_id and c.tenant_id = f.tenant_id
    where c.deleted_at is null and c.status <> 'draft'
    group by f.page_id
    having count(distinct f.tenant_id) > 1
"""

_CUENTAS_DUPLICADAS = """
    select a.ig_user_id, array_agg(distinct a.tenant_id::text) as tenants
    from ig_accounts a
    join meta_connections c on c.id = a.connection_id and c.tenant_id = a.tenant_id
    where c.deleted_at is null and c.status <> 'draft'
    group by a.ig_user_id
    having count(distinct a.tenant_id) > 1
"""

_FUNCIONES = """
CREATE OR REPLACE FUNCTION resolve_tenant_for_page(p_page_id text)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT CASE WHEN count(DISTINCT f.tenant_id) = 1 THEN (array_agg(f.tenant_id))[1] END
    FROM fb_pages f
    JOIN meta_connections c ON c.id = f.connection_id AND c.tenant_id = f.tenant_id
    WHERE f.page_id = p_page_id AND c.deleted_at IS NULL AND c.status <> 'draft';
$$;

CREATE OR REPLACE FUNCTION resolve_tenant_for_ig_user(p_ig_user_id text)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT CASE WHEN count(DISTINCT a.tenant_id) = 1 THEN (array_agg(a.tenant_id))[1] END
    FROM ig_accounts a
    JOIN meta_connections c ON c.id = a.connection_id AND c.tenant_id = a.tenant_id
    WHERE a.ig_user_id = p_ig_user_id AND c.deleted_at IS NULL AND c.status <> 'draft';
$$;

CREATE OR REPLACE FUNCTION meta_messaging_asset_claimed_by_other_tenant(
    p_page_id text, p_ig_user_id text, p_tenant uuid
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM fb_pages f
        JOIN meta_connections c ON c.id = f.connection_id AND c.tenant_id = f.tenant_id
        WHERE f.page_id = p_page_id AND c.deleted_at IS NULL AND c.status <> 'draft'
          AND f.tenant_id IS DISTINCT FROM p_tenant
    ) OR EXISTS (
        SELECT 1 FROM ig_accounts a
        JOIN meta_connections c ON c.id = a.connection_id AND c.tenant_id = a.tenant_id
        WHERE a.ig_user_id = p_ig_user_id AND c.deleted_at IS NULL AND c.status <> 'draft'
          AND a.tenant_id IS DISTINCT FROM p_tenant
    );
$$;

-- Solo el runtime las necesita; el dueño conserva EXECUTE por ser owner (ADR-0039 §3).
REVOKE ALL ON FUNCTION resolve_tenant_for_page(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION resolve_tenant_for_ig_user(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION meta_messaging_asset_claimed_by_other_tenant(text, text, uuid) FROM PUBLIC;
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        GRANT EXECUTE ON FUNCTION resolve_tenant_for_page(text) TO app_runtime;
        GRANT EXECUTE ON FUNCTION resolve_tenant_for_ig_user(text) TO app_runtime;
        GRANT EXECUTE ON FUNCTION meta_messaging_asset_claimed_by_other_tenant(text, text, uuid) TO app_runtime;
    END IF;
END $do$;
"""

# Definiciones previas (0021), para el downgrade.
_FUNCIONES_PREVIAS = """
CREATE OR REPLACE FUNCTION resolve_tenant_for_page(p_page_id text)
RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
    SELECT tenant_id FROM fb_pages WHERE page_id = p_page_id LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION resolve_tenant_for_ig_user(p_ig_user_id text)
RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
    SELECT tenant_id FROM ig_accounts WHERE ig_user_id = p_ig_user_id LIMIT 1;
$$;

GRANT EXECUTE ON FUNCTION resolve_tenant_for_page(text) TO PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_tenant_for_ig_user(text) TO PUBLIC;
"""


def upgrade() -> None:
    bind = op.get_bind()
    if not bind.execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError(
            "0104: el rol de migraciones no es superusuario ni tiene BYPASSRLS. Las funciones "
            "SECURITY DEFINER de ruteo y de reclamo verían 0 filas por la RLS forzada "
            "(ADR-0039, Consecuencias). Corre la migración con el dueño de la base."
        )

    for page_id, tenants in bind.execute(sa.text(_PAGINAS_DUPLICADAS)).all():
        log.warning(
            "0104: la Página %s la tienen %d negocios (%s); deja de rutear hasta borrar la conexión que sobra",
            page_id, len(tenants), ", ".join(tenants),
        )
    for ig_user_id, tenants in bind.execute(sa.text(_CUENTAS_DUPLICADAS)).all():
        log.warning(
            "0104: la cuenta de Instagram %s la tienen %d negocios (%s); deja de rutear hasta borrar la conexión que sobra",
            ig_user_id, len(tenants), ", ".join(tenants),
        )

    op.execute(_FUNCIONES)


def downgrade() -> None:
    op.execute("drop function if exists meta_messaging_asset_claimed_by_other_tenant(text, text, uuid)")
    op.execute(_FUNCIONES_PREVIAS)
