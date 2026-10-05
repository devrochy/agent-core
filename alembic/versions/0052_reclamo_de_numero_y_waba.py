"""Un número o una WABA de Meta pertenecen a un solo negocio (ADR-0095).

Hasta ahora un negocio podía registrar la WABA o el número de otro y recibir sus
mensajes:

- `resolve_tenant_for_phone` (0003) solo miraba `channel_connections`, así que un
  número cargado en `phone_numbers` por otro negocio no contaba (y comparaba con el
  `external_id` de cualquier tipo de conexión: un correo, un calendario);
- `resolve_tenant_for_waba` (0021) devolvía el primero que encontrara (`LIMIT 1` sin
  orden) y contaba también las WABAs de conexiones borradas.

Desde aquí las dos funciones de ruteo miran solo conexiones vivas (y, en
`channel_connections`, solo las de WhatsApp por Meta), cuentan los
negocios que reclaman el activo y devuelven NULL si hay cero o más de uno: un evento
ambiguo queda como `unresolved_tenant` en `webhook_events` en vez de ir a un negocio
cualquiera. "Viva" es no borrada y fuera de `draft`: una conexión en `draft` es un
Embedded Signup que nunca canjeó su `code`, y sus filas las escribía el navegador sin
verificar (el alta nueva ya no escribe nada antes del canje). `meta_asset_claimed_by_other_tenant` responde a las altas si otro negocio
ya tiene la WABA o el número; devuelve un booleano y nunca dice cuál (mismo criterio
que ADR-0039). `p_tenant` puede ser NULL (alta de un tenant nuevo): se compara con
`IS DISTINCT FROM`, así cualquier dueño cuenta como "otro".

RLS: `waba_accounts`, `phone_numbers`, `channel_connections` y `meta_connections`
tienen RLS forzada. Una función SECURITY DEFINER cuyo dueño no sea superusuario ni
tenga `BYPASSRLS` ve 0 filas: el ruteo daría siempre NULL y el reclamo siempre
"libre". La migración aborta si el rol que la corre no puede saltar RLS (ADR-0039,
Consecuencias), y con ese mismo rol lista en el log:

- los activos que hoy reclaman dos negocios: dejan de rutear por WABA o por número
  hasta que se borre la conexión que sobra;
- las filas de conexiones en `draft`, que dejan de contar;
- los activos de altas manuales, que se guardaron sin verificar que el número fuera de
  la WABA: hay que revisarlos contra Graph.

Las tres funciones fijan `search_path = public, pg_temp` (una tabla temporal no puede
tapar las reales) y solo las ejecuta `app_runtime`: los dos resolvedores conservaban
EXECUTE para PUBLIC desde 0003/0021.

Revision ID: 0052
Revises: 0051
Create Date: 2026-10-05
"""
from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"

# Activos que hoy reclaman dos o más negocios con conexiones vivas: con la regla nueva
# dejan de rutear (NULL). Se listan para corregirlos a mano.
_WABAS_DUPLICADAS = """
    select w.waba_id, array_agg(distinct w.tenant_id::text) as tenants
    from waba_accounts w
    join meta_connections c on c.id = w.connection_id and c.tenant_id = w.tenant_id
    where c.deleted_at is null and c.status <> 'draft'
    group by w.waba_id
    having count(distinct w.tenant_id) > 1
"""

_NUMEROS_DUPLICADOS = """
    select t.phone_number_id, array_agg(distinct t.tenant_id::text) as tenants
    from (
        select cc.external_id as phone_number_id, cc.tenant_id
        from channel_connections cc
        where cc.channel = 'whatsapp' and cc.provider = 'meta' and cc.deleted_at is null
        union all
        select p.phone_number_id, p.tenant_id
        from phone_numbers p
        join meta_connections c on c.id = p.connection_id and c.tenant_id = p.tenant_id
        where c.deleted_at is null and c.status <> 'draft'
    ) t
    group by t.phone_number_id
    having count(distinct t.tenant_id) > 1
"""

# Filas que dejan de contar: conexiones vivas en `draft` (Embedded Signup sin canje).
_FILAS_SIN_CANJE = """
    select c.tenant_id::text, c.id::text, 'WABA' as activo, w.waba_id as id_meta
    from waba_accounts w
    join meta_connections c on c.id = w.connection_id and c.tenant_id = w.tenant_id
    where c.deleted_at is null and c.status = 'draft'
    union all
    select c.tenant_id::text, c.id::text, 'número', p.phone_number_id
    from phone_numbers p
    join meta_connections c on c.id = p.connection_id and c.tenant_id = p.tenant_id
    where c.deleted_at is null and c.status = 'draft'
"""

# Activos de altas manuales: se guardaron sin verificar el par WABA-número en Graph.
_ALTAS_MANUALES = """
    select c.tenant_id::text, c.id::text,
           coalesce(string_agg(distinct w.waba_id, ', '), '-'),
           coalesce(string_agg(distinct p.phone_number_id, ', '), '-')
    from meta_connections c
    left join waba_accounts w on w.connection_id = c.id and w.tenant_id = c.tenant_id
    left join phone_numbers p on p.connection_id = c.id and p.tenant_id = c.tenant_id
    where c.deleted_at is null and c.status <> 'draft' and c.origin = 'manual'
    group by c.tenant_id, c.id
"""

_FUNCIONES = """
CREATE OR REPLACE FUNCTION resolve_tenant_for_phone(p_phone_number_id text)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT CASE WHEN count(DISTINCT t.tenant_id) = 1 THEN (array_agg(t.tenant_id))[1] END
    FROM (
        SELECT cc.tenant_id FROM channel_connections cc
        WHERE cc.external_id = p_phone_number_id AND cc.channel = 'whatsapp'
          AND cc.provider = 'meta' AND cc.deleted_at IS NULL
        UNION ALL
        SELECT p.tenant_id FROM phone_numbers p
        JOIN meta_connections c ON c.id = p.connection_id AND c.tenant_id = p.tenant_id
        WHERE p.phone_number_id = p_phone_number_id AND c.deleted_at IS NULL AND c.status <> 'draft'
    ) t;
$$;

CREATE OR REPLACE FUNCTION resolve_tenant_for_waba(p_waba_id text)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT CASE WHEN count(DISTINCT w.tenant_id) = 1 THEN (array_agg(w.tenant_id))[1] END
    FROM waba_accounts w
    JOIN meta_connections c ON c.id = w.connection_id AND c.tenant_id = w.tenant_id
    WHERE w.waba_id = p_waba_id AND c.deleted_at IS NULL AND c.status <> 'draft';
$$;

CREATE OR REPLACE FUNCTION meta_asset_claimed_by_other_tenant(
    p_waba_id text, p_phone_number_id text, p_tenant uuid
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT EXISTS (
        SELECT 1 FROM waba_accounts w
        JOIN meta_connections c ON c.id = w.connection_id AND c.tenant_id = w.tenant_id
        WHERE w.waba_id = p_waba_id AND c.deleted_at IS NULL AND c.status <> 'draft'
          AND w.tenant_id IS DISTINCT FROM p_tenant
    ) OR EXISTS (
        SELECT 1 FROM phone_numbers p
        JOIN meta_connections c ON c.id = p.connection_id AND c.tenant_id = p.tenant_id
        WHERE p.phone_number_id = p_phone_number_id AND c.deleted_at IS NULL AND c.status <> 'draft'
          AND p.tenant_id IS DISTINCT FROM p_tenant
    ) OR EXISTS (
        SELECT 1 FROM channel_connections cc
        WHERE cc.external_id = p_phone_number_id AND cc.channel = 'whatsapp'
          AND cc.provider = 'meta' AND cc.deleted_at IS NULL
          AND cc.tenant_id IS DISTINCT FROM p_tenant
    );
$$;

-- Solo el runtime las necesita; el dueño conserva EXECUTE por ser owner (ADR-0039 §3).
REVOKE ALL ON FUNCTION resolve_tenant_for_phone(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION resolve_tenant_for_waba(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION meta_asset_claimed_by_other_tenant(text, text, uuid) FROM PUBLIC;
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        GRANT EXECUTE ON FUNCTION resolve_tenant_for_phone(text) TO app_runtime;
        GRANT EXECUTE ON FUNCTION resolve_tenant_for_waba(text) TO app_runtime;
        GRANT EXECUTE ON FUNCTION meta_asset_claimed_by_other_tenant(text, text, uuid) TO app_runtime;
    END IF;
END $do$;
"""

# Definiciones previas (0003 y 0021), para el downgrade.
_FUNCIONES_PREVIAS = """
CREATE OR REPLACE FUNCTION resolve_tenant_for_phone(p_phone_number_id text)
RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public AS $$
    SELECT tenant_id FROM channel_connections
    WHERE external_id = p_phone_number_id AND deleted_at IS NULL
    LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION resolve_tenant_for_waba(p_waba_id text)
RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public AS $$
    SELECT tenant_id FROM waba_accounts WHERE waba_id = p_waba_id LIMIT 1;
$$;

GRANT EXECUTE ON FUNCTION resolve_tenant_for_phone(text) TO PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_tenant_for_waba(text) TO PUBLIC;
"""


def upgrade() -> None:
    bind = op.get_bind()
    if not bind.execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError(
            "0052: el rol de migraciones no es superusuario ni tiene BYPASSRLS. Las funciones "
            "SECURITY DEFINER de ruteo y de reclamo verían 0 filas por la RLS forzada "
            "(ADR-0039, Consecuencias). Corre la migración con el dueño de la base."
        )

    for waba_id, tenants in bind.execute(sa.text(_WABAS_DUPLICADAS)).all():
        log.warning(
            "0052: la WABA %s la tienen %d negocios (%s); deja de rutear por WABA",
            waba_id, len(tenants), ", ".join(tenants),
        )
    for phone_number_id, tenants in bind.execute(sa.text(_NUMEROS_DUPLICADOS)).all():
        log.warning(
            "0052: el número %s lo tienen %d negocios (%s); deja de rutear por número",
            phone_number_id, len(tenants), ", ".join(tenants),
        )

    for tenant_id, connection_id, activo, id_meta in bind.execute(sa.text(_FILAS_SIN_CANJE)).all():
        log.warning(
            "0052: %s %s de la conexión %s (tenant %s) está en draft (Embedded Signup sin canje); "
            "deja de contar para el ruteo y el reclamo",
            activo, id_meta, connection_id, tenant_id,
        )
    for tenant_id, connection_id, wabas, numeros in bind.execute(sa.text(_ALTAS_MANUALES)).all():
        log.warning(
            "0052: alta manual sin verificar (conexión %s, tenant %s): WABA %s, números %s; "
            "comprobar en Graph que el número es de la WABA",
            connection_id, tenant_id, wabas, numeros,
        )

    op.execute(_FUNCIONES)


def downgrade() -> None:
    op.execute("drop function if exists meta_asset_claimed_by_other_tenant(text, text, uuid)")
    op.execute(_FUNCIONES_PREVIAS)
