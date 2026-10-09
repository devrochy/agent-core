"""Borrado lógico de números y WABAs; plantillas únicas por negocio (HU-CONN-03, ADR-0122).

- `phone_numbers.deleted_at` y `waba_accounts.deleted_at`: eliminar una conexión ya no borra en
  duro sus números y WABAs (ni, por `ON DELETE CASCADE`, sus plantillas y `pricing_daily`).
- Backfill: los números y WABAs de conexiones ya eliminadas (p. ej. las que retiró 0052) quedan
  con el `deleted_at` de su conexión. Con RLS forzada, un `UPDATE` sin `BYPASSRLS` no falla:
  actualiza 0 filas (ADR-0094 §5). Por eso se exige el rol dueño.
- `phone_numbers.phone_number_id`: el único global pasa a **parcial** (solo filas vivas: otro
  negocio puede reclamar un número eliminado) más un único **completo** por negocio
  `(tenant_id, phone_number_id)`, que obliga a revivir la fila al reconectar.
- `message_templates`: el único de `meta_template_id` pasa de global a por negocio.
- Funciones de ruteo y de reclamo (0052): ignoran números y WABAs eliminados.
- `resolve_tenant_for_message_template` (0026) se elimina: ya no se usa.

Tablas chicas: los `ALTER` toman `ACCESS EXCLUSIVE` unos milisegundos. `lock_timeout` evita
quedar en cola detrás de una transacción larga y frenar todo el inbound mientras tanto.

Irreversible: con números eliminados duplicados, el único global de antes no se puede recrear.

Revision ID: 0063
Revises: 0062
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"

_BACKFILL = """
update {tabla} t set deleted_at = c.deleted_at
from meta_connections c
where c.id = t.connection_id and c.tenant_id = t.tenant_id
  and c.deleted_at is not null and t.deleted_at is null
"""

# El único de `phone_number_id` se creó con `unique=True` (0015): se busca por columnas, no
# por el nombre que le puso Postgres.
_SOLTAR_UNICO_GLOBAL_DEL_NUMERO = """
DO $do$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT con.conname FROM pg_constraint con
        JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ANY (con.conkey)
        WHERE con.conrelid = 'phone_numbers'::regclass AND con.contype = 'u'
          AND array_length(con.conkey, 1) = 1 AND a.attname = 'phone_number_id'
    LOOP
        EXECUTE format('ALTER TABLE phone_numbers DROP CONSTRAINT %I', r.conname);
    END LOOP;
END $do$;
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
        WHERE p.phone_number_id = p_phone_number_id AND p.deleted_at IS NULL
          AND c.deleted_at IS NULL AND c.status <> 'draft'
    ) t;
$$;

CREATE OR REPLACE FUNCTION resolve_tenant_for_waba(p_waba_id text)
RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
    SELECT CASE WHEN count(DISTINCT w.tenant_id) = 1 THEN (array_agg(w.tenant_id))[1] END
    FROM waba_accounts w
    JOIN meta_connections c ON c.id = w.connection_id AND c.tenant_id = w.tenant_id
    WHERE w.waba_id = p_waba_id AND w.deleted_at IS NULL AND c.deleted_at IS NULL AND c.status <> 'draft';
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


def upgrade() -> None:
    bind = op.get_bind()
    if not bind.execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError(
            "0063: el rol de migraciones no es superusuario ni tiene BYPASSRLS. El backfill de "
            "`deleted_at` actualizaría 0 filas por la RLS forzada (ADR-0094 §5). Corre la "
            "migración con el dueño de la base."
        )
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.add_column("phone_numbers", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("waba_accounts", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(_BACKFILL.format(tabla="phone_numbers"))
    op.execute(_BACKFILL.format(tabla="waba_accounts"))

    # Primero los índices nuevos, después se suelta el viejo: nunca queda sin único.
    op.create_index(
        "uq_phone_numbers_phone_number_id_vivo",
        "phone_numbers",
        ["phone_number_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )
    op.create_unique_constraint("uq_phone_numbers_tenant_phone", "phone_numbers", ["tenant_id", "phone_number_id"])
    op.execute(_SOLTAR_UNICO_GLOBAL_DEL_NUMERO)

    op.create_index(
        "uq_message_templates_tenant_meta_id",
        "message_templates",
        ["tenant_id", "meta_template_id"],
        unique=True,
        postgresql_where=sa.text("meta_template_id is not null"),
    )
    op.drop_index("uq_message_templates_meta_id", table_name="message_templates")

    op.execute(_FUNCIONES)
    # Ya no la usa nadie (los webhooks de plantillas buscan bajo el scope del negocio) y su
    # `LIMIT 1` global es ambiguo con ids repetidos entre negocios.
    op.execute("drop function if exists resolve_tenant_for_message_template(text)")


def downgrade() -> None:
    raise RuntimeError(
        "0063 es irreversible: con números eliminados que otro negocio ya reclamó, el único global "
        "de `phone_numbers.phone_number_id` no se puede recrear. Restaurar desde un respaldo."
    )
