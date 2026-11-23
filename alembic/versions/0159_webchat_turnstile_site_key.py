"""Chat web: site key pública de Turnstile en el resolvedor del widget.

Turnstile necesita dos claves: la **site key** (pública, la usa el navegador para pedir el
token) y la **secret key** (privada, la usa el backend en `siteverify`). Hasta ahora la conexión
`webchat` solo guardaba la secreta, así que el widget no podía renderizar el desafío y, con la
protección encendida, todos los POST terminaban en 403.

La site key no es un secreto: va en `channel_connections.settings -> 'turnstile_site_key'` y el
resolvedor público la devuelve junto a `has_secret`. El secreto sigue cifrado en
`credentials_encrypted` y nunca sale de la API.

`webchat_resolve` cambia de firma (una columna más), así que se recrea: `CREATE OR REPLACE` no
puede cambiar el tipo de retorno. Sigue siendo `SECURITY DEFINER` con `search_path` fijo y
`EXECUTE` solo para `app_runtime`; el dueño de la función tiene que saltar la RLS (superusuario o
BYPASSRLS), como en la 0086, o vería 0 filas.

Solo toca una función: sin bloqueos sobre tablas de negocio.

Revision ID: 0159
Revises: 0158
Create Date: 2026-11-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0159"
down_revision = "0158"
branch_labels = None
depends_on = None

_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"

_RESOLVEDOR = """
CREATE OR REPLACE FUNCTION webchat_resolve(p_key text)
RETURNS TABLE(
    tenant_id uuid,
    connection_id uuid,
    active boolean,
    allowed_origins jsonb,
    has_secret boolean,
    turnstile_site_key text
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT cc.tenant_id,
           cc.id,
           cc.active,
           cc.settings -> 'allowed_origins',
           cc.credentials_encrypted IS NOT NULL,
           cc.settings ->> 'turnstile_site_key'
      FROM public.channel_connections cc
     WHERE cc.type_id = 'webchat'
       AND cc.external_id = p_key
       AND cc.deleted_at IS NULL;
$$;
REVOKE ALL ON FUNCTION webchat_resolve(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION webchat_resolve(text) TO app_runtime;
"""

_RESOLVEDOR_ANTERIOR = """
CREATE OR REPLACE FUNCTION webchat_resolve(p_key text)
RETURNS TABLE(tenant_id uuid, connection_id uuid, active boolean, allowed_origins jsonb, has_secret boolean)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $$
    SELECT cc.tenant_id, cc.id, cc.active, cc.settings -> 'allowed_origins', cc.credentials_encrypted IS NOT NULL
      FROM public.channel_connections cc
     WHERE cc.type_id = 'webchat'
       AND cc.external_id = p_key
       AND cc.deleted_at IS NULL;
$$;
REVOKE ALL ON FUNCTION webchat_resolve(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION webchat_resolve(text) TO app_runtime;
"""


def upgrade() -> None:
    bind = op.get_bind()
    if not bind.execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError(
            "0159: el rol de migraciones no es superusuario ni tiene BYPASSRLS. El resolvedor "
            "SECURITY DEFINER del chat web vería 0 filas por la RLS forzada. Corre la migración "
            "con el dueño de la base."
        )
    # La firma cambia (columna nueva): hay que soltar la función antes de recrearla.
    op.execute("DROP FUNCTION IF EXISTS webchat_resolve(text)")
    op.execute(_RESOLVEDOR)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS webchat_resolve(text)")
    op.execute(_RESOLVEDOR_ANTERIOR)
