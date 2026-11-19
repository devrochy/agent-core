"""Un token de push (un teléfono) queda activo en un solo negocio y a nombre de una sola cuenta (ADR-0168).

`push_tokens` es único por `(tenant_id, token)` entre las filas activas y tiene RLS
forzada, así que desde el negocio activo no se ve ningún otro negocio. Resultado visto en
test (2026-10-26): un teléfono con sesión en «Cobranza Demo» seguía recibiendo los avisos
de `solvershub` bajo la cuenta que lo registró allí semanas antes y nunca lo dio de baja.

`push_token_release_elsewhere(p_token, p_keep_tenant, p_keep_admin)` da de baja (soft
delete, invariante 8) las filas ACTIVAS de ese `token` exacto que no sean la
`(p_keep_tenant, p_keep_admin)` que acaba de registrarlo: las de otros negocios y las de
otras cuentas. Devuelve cuántas liberó. Es el único cruce de negocios y se acota así:

- solo puede poner `deleted_at` (y `updated_at`): no lee ni devuelve filas, ids ni cuentas,
  solo un conteo; no inserta ni borra físicamente;
- solo actúa sobre el token exacto que recibe, que únicamente conoce la app instalada en
  ese teléfono (el token no viaja en ninguna respuesta de la API);
- si algún parámetro es NULL o el token viene vacío, no toca nada (un NULL en
  `p_keep_*` liberaría también la fila que se acaba de registrar);
- fija `search_path = public, pg_temp`, revoca EXECUTE a PUBLIC y lo concede solo a
  `app_runtime` (ADR-0039 §3).

No crea tablas, así que no hay RLS nueva. `push_tokens` tiene RLS forzada: una función
SECURITY DEFINER cuyo dueño no sea superusuario ni tenga `BYPASSRLS` vería 0 filas y
liberaría nada. La migración aborta si el rol que la corre no puede saltar RLS (ADR-0039,
Consecuencias), igual que 0104.

Revision ID: 0109
Revises: 0108
Create Date: 2026-10-26
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None

_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"

_FUNCION = """
CREATE OR REPLACE FUNCTION push_token_release_elsewhere(
    p_token text, p_keep_tenant uuid, p_keep_admin uuid
)
RETURNS integer LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    WITH liberados AS (
        UPDATE public.push_tokens
        SET deleted_at = now(), updated_at = now()
        WHERE token = p_token
          AND deleted_at IS NULL
          AND p_token <> ''
          AND p_keep_tenant IS NOT NULL
          AND p_keep_admin IS NOT NULL
          AND (tenant_id <> p_keep_tenant OR admin_id <> p_keep_admin)
        RETURNING 1
    )
    SELECT count(*)::integer FROM liberados;
$$;

REVOKE ALL ON FUNCTION push_token_release_elsewhere(text, uuid, uuid) FROM PUBLIC;
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        GRANT EXECUTE ON FUNCTION push_token_release_elsewhere(text, uuid, uuid) TO app_runtime;
    END IF;
END $do$;
"""


def upgrade() -> None:
    bind = op.get_bind()
    if not bind.execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError(
            "0109: el rol de migraciones no es superusuario ni tiene BYPASSRLS. La función "
            "SECURITY DEFINER de push_tokens vería 0 filas por la RLS forzada y no liberaría "
            "nada (ADR-0039, Consecuencias). Corre la migración con el dueño de la base."
        )
    op.execute(_FUNCION)


def downgrade() -> None:
    op.execute("drop function if exists push_token_release_elsewhere(text, uuid, uuid)")
