"""La conexión canónica de un número de Meta no rutea ni reclama el número (HU-AOS-11, ADR-0115).

Desde ADR-0115 cada número de Meta tiene su fila en `channel_connections`, ligada a su cuenta
(`meta_connection_id`) y sin estado propio: la pausa y la baja son las de la cuenta (ADR-0122),
así que la fila sigue viva cuando la cuenta se elimina. `resolve_tenant_for_phone` y
`meta_asset_claimed_by_other_tenant` (0063) tratan toda fila viva de `channel_connections` de
WhatsApp por Meta como dueña del número (el criterio de las conexiones manuales). Con las filas
canónicas eso cambiaría el ruteo y el reclamo:

- un número dado de baja seguiría reclamado por el negocio anterior y otro no podría conectarlo;
- su webhook seguiría ruteando al negocio anterior (o a ninguno, con dos dueños).

Las dos funciones pasan a mirar solo las filas SIN cuenta (`meta_connection_id IS NULL`): la
dueña del número de Meta sigue siendo su fila de `phone_numbers` con su cuenta, como antes.
Mismo cuerpo que 0063 más ese filtro; mismos permisos (solo `app_runtime`, ADR-0039 §3).

Sin datos: el backfill de las conexiones va por CLI (`agent-core db backfill-conexiones`), no
por migración. Reversible: el `downgrade` deja las definiciones de 0063, pero si ya hay filas
canónicas eso devuelve el defecto de arriba (una fila que sobrevive a la baja de su cuenta
vuelve a reclamar el número y a rutear su webhook al negocio anterior). Por eso el `downgrade`
se niega si existe alguna fila con `meta_connection_id` (exige un rol que vea todos los
negocios: con la RLS forzada, otro rol vería cero filas y bajaría a ciegas).

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-10
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None

_FILTRO = "\n          AND cc.meta_connection_id IS NULL"


def _funciones(filtro: str) -> str:
    return f"""
CREATE OR REPLACE FUNCTION resolve_tenant_for_phone(p_phone_number_id text)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT CASE WHEN count(DISTINCT t.tenant_id) = 1 THEN (array_agg(t.tenant_id))[1] END
    FROM (
        SELECT cc.tenant_id FROM channel_connections cc
        WHERE cc.external_id = p_phone_number_id AND cc.channel = 'whatsapp'
          AND cc.provider = 'meta' AND cc.deleted_at IS NULL{filtro}
        UNION ALL
        SELECT p.tenant_id FROM phone_numbers p
        JOIN meta_connections c ON c.id = p.connection_id AND c.tenant_id = p.tenant_id
        WHERE p.phone_number_id = p_phone_number_id AND p.deleted_at IS NULL
          AND c.deleted_at IS NULL AND c.status <> 'draft'
    ) t;
$$;

CREATE OR REPLACE FUNCTION meta_asset_claimed_by_other_tenant(
    p_waba_id text, p_phone_number_id text, p_tenant uuid
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM waba_accounts w
        JOIN meta_connections c ON c.id = w.connection_id AND c.tenant_id = w.tenant_id
        WHERE w.waba_id = p_waba_id AND w.deleted_at IS NULL AND c.deleted_at IS NULL AND c.status <> 'draft'
          AND w.tenant_id IS DISTINCT FROM p_tenant
    ) OR EXISTS (
        SELECT 1 FROM phone_numbers p
        JOIN meta_connections c ON c.id = p.connection_id AND c.tenant_id = p.tenant_id
        WHERE p.phone_number_id = p_phone_number_id AND p.deleted_at IS NULL
          AND c.deleted_at IS NULL AND c.status <> 'draft'
          AND p.tenant_id IS DISTINCT FROM p_tenant
    ) OR EXISTS (
        SELECT 1 FROM channel_connections cc
        WHERE cc.external_id = p_phone_number_id AND cc.channel = 'whatsapp'
          AND cc.provider = 'meta' AND cc.deleted_at IS NULL{filtro}
          AND cc.tenant_id IS DISTINCT FROM p_tenant
    );
$$;

REVOKE ALL ON FUNCTION resolve_tenant_for_phone(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION meta_asset_claimed_by_other_tenant(text, text, uuid) FROM PUBLIC;
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        GRANT EXECUTE ON FUNCTION resolve_tenant_for_phone(text) TO app_runtime;
        GRANT EXECUTE ON FUNCTION meta_asset_claimed_by_other_tenant(text, text, uuid) TO app_runtime;
    END IF;
END $do$;
"""


def upgrade() -> None:
    op.execute(_funciones(_FILTRO))


def downgrade() -> None:
    bind = op.get_bind()
    puede_ver_todo = bind.execute(
        sa.text("select rolsuper or rolbypassrls from pg_roles where rolname = current_user")
    ).scalar()
    if not puede_ver_todo:
        raise RuntimeError("0073: el downgrade necesita el dueño de la base (BYPASSRLS) para revisar las filas.")
    if bind.execute(sa.text("select exists (select 1 from channel_connections where meta_connection_id is not null)")).scalar():
        raise RuntimeError(
            "0073: hay conexiones canónicas (`meta_connection_id`); con las funciones de 0063 reclamarían "
            "números dados de baja y desviarían su webhook. Desligarlas antes de bajar."
        )
    op.execute(_funciones(""))
