"""Chat web como conexión `webchat`: resolvedor público por la clave (HU-AOS-11 parte 2, ADR-0115 §4).

- Tipo `webchat` en el catálogo (`connection_types`), con el mismo `upsert_connection_type()` de la
  `0038`: la fila tiene que existir antes del arranque del `api` (que la sincroniza desde
  `core/gateway/connection_types.py`), porque `agent_instance_checks()` (0069) rechaza una instancia
  en una conexión sin tipo conversacional y el backfill puede correr antes.
- `webchat_resolve(p_key)` SECURITY DEFINER: la superficie pública `/public/web-chat/{key}` no tiene
  negocio fijado y `channel_connections` tiene RLS forzada. Devuelve de la conexión viva cuya clave
  es `p_key` solo lo no secreto: `tenant_id`, `connection_id`, `active`, los dominios permitidos
  (`settings -> 'allowed_origins'`, no el `settings` entero) y si tiene secreto de Turnstile (`credentials_encrypted IS NOT NULL`, nunca su valor:
  el POST lo lee después con RLS, en el `tenant_scope` del negocio). Igualdad sobre el predicado de
  `uq_cc_webchat_key` (0071, índice único global que incluye las borradas): a lo sumo una fila, sin
  `LIMIT`. `search_path` fijo y `EXECUTE` solo para `app_runtime` (no PUBLIC).
- `web_chat_resolve` (0048, sobre `app_settings`) se conserva: el endpoint cae a ella para los
  negocios que todavía no tienen su conexión (antes del backfill). Se elimina en el contract.

Solo crea una función y una fila del catálogo: sin bloqueos sobre tablas de negocio.

El dueño de la función tiene que saltar la RLS (superusuario o BYPASSRLS, como el
`agent_core_migrations` de producción): si no, vería 0 filas y todo widget daría 404. La migración
aborta en ese caso, como la `0052`.

Revision ID: 0086
Revises: 0082
Create Date: 2026-10-11
"""

from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0086"
down_revision = "0082"
branch_labels = None
depends_on = None

_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"

# Debe coincidir con `core/gateway/connection_types.py` (el arranque lo vuelve a sincronizar).
_WEBCHAT = {
    "id": "webchat",
    "category": "comunicacion",
    "channel": "web",
    "label": "Chat web",
    "provider": "web",
    "icon": "bi-chat-dots",
    "description": "Widget de chat para tu sitio web",
    "credentials_schema": json.dumps({}),
    "external_id_key": "",
    "produces_conversations": True,
    "available": True,
    "setup_kind": "wizard",
    "sort_order": 35,
}

# "A lo sumo una fila sin LIMIT" depende de `uq_cc_webchat_key` (0071, CONCURRENTLY): si quedó
# inválido o no existe, la unicidad no está garantizada y la migración aborta.
_INDICE_VALIDO = """
    select coalesce((select i.indisvalid and i.indisunique from pg_index i
                      where i.indexrelid = to_regclass('public.uq_cc_webchat_key')), false)
"""

_RESOLVEDOR = """
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
            "0086: el rol de migraciones no es superusuario ni tiene BYPASSRLS. El resolvedor "
            "SECURITY DEFINER del chat web vería 0 filas por la RLS forzada. Corre la migración "
            "con el dueño de la base."
        )
    if not bind.execute(sa.text(_INDICE_VALIDO)).scalar():
        raise RuntimeError(
            "0086: falta el índice único `uq_cc_webchat_key` (0071) o quedó inválido: recréalo antes de "
            "crear el resolvedor del chat web."
        )
    op.execute(
        sa.text(
            "select upsert_connection_type("
            ":id, :category, :channel, :label, :provider, :icon, :description, "
            "cast(:credentials_schema as jsonb), :external_id_key, "
            ":produces_conversations, :available, :setup_kind, :sort_order)"
        ).bindparams(**_WEBCHAT)
    )
    op.execute(_RESOLVEDOR)


def downgrade() -> None:
    # La fila `webchat` del catálogo se queda: `sync_connection_types` tampoco borra tipos y las
    # conexiones que la usan siguen siendo válidas para el código anterior (que lee `app_settings`).
    op.execute("DROP FUNCTION IF EXISTS webchat_resolve(text)")
