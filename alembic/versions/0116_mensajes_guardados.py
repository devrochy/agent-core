"""Mensajes guardados: respuestas rápidas del operador reutilizables entre canales (ADR-0178, pista D4).

Una tabla de NEGOCIO: `tenant_id` + RLS habilitada y forzada + policy de aislamiento (invariante 1) e índice único
parcial que empieza por `tenant_id`.

- `mensajes_guardados`: un texto libre con variables del dialecto común (`{cliente.nombre}`, `core/kernel/mensajes`)
  que el operador reutiliza. `clave` única por negocio entre los vivos (índice parcial `where deleted_at is null`:
  el borrado es lógico, invariante 8, y libera la clave). `canales` son `ConnectionType.id` (`whatsapp_meta`,
  `webchat`…); vacío = todos los canales. El catálogo de tipos vive en código, no en la base, así que la lista
  permitida se valida en el servicio, no con un CHECK. `created_by`/`updated_by` son el id del admin que escribió,
  sin FK (como el resto de tablas con autor). El largo de `texto` y `titulo` lo acota el esquema con holgura sobre
  el tope de la API (subir el tope de la API no exige otra migración).

Sin DELETE para `app_runtime`: el borrado lógico es un UPDATE (como 0112); solo `purge_tenant` borra, y no necesita
cambios (descubre las tablas por su columna `tenant_id`, 0072).

Aditiva y reversible: tabla nueva y vacía (no hay nada que migrar), sin reescritura ni bloqueo; el downgrade la
elimina (se pierden los mensajes guardados). No toca `plugin_configs` ni ningún dato existente.

Revision ID: 0116
Revises: 0115
Create Date: 2026-10-28
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID

from alembic import op

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_UUID = UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_AHORA = sa.text("now()")
_TABLA = "mensajes_guardados"


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    # Sin DELETE: el borrado lógico es un UPDATE. Los default privileges de 0003 le dan DELETE al rol de la app,
    # así que se revoca antes de conceder (como 0112).
    op.execute(f"revoke all on {table} from app_runtime")
    op.execute(f"grant select, insert, update on {table} to app_runtime")


def upgrade() -> None:
    op.create_table(
        _TABLA,
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("clave", sa.Text(), nullable=False),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("texto", sa.Text(), nullable=False),
        sa.Column("canales", ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'::text[]")),
        sa.Column("estado", sa.Text(), nullable=False, server_default="borrador"),
        sa.Column("created_by", _UUID, nullable=True),
        sa.Column("updated_by", _UUID, nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("updated_at", _TS, nullable=False, server_default=_AHORA),
        sa.UniqueConstraint("tenant_id", "id", name="uq_mensajes_guardados_tenant_id_id"),
        sa.CheckConstraint("estado in ('borrador','activo','archivado')", name="ck_mensajes_guardados_estado"),
        sa.CheckConstraint("clave ~ '^[a-z0-9_]{1,40}$'", name="ck_mensajes_guardados_clave"),
        sa.CheckConstraint("char_length(titulo) between 1 and 120 and titulo !~ '^\\s*$'", name="ck_mensajes_guardados_titulo"),
        sa.CheckConstraint("char_length(texto) between 1 and 4096 and texto !~ '^\\s*$'", name="ck_mensajes_guardados_texto"),
        sa.CheckConstraint("cardinality(canales) <= 20 and (array_ndims(canales) is null or array_ndims(canales) = 1) "
            "and array_position(canales, null) is null and array_position(canales, '') is null", name="ck_mensajes_guardados_canales"),
    )
    op.create_index(
        "uq_mensajes_guardados_tenant_clave",
        _TABLA,
        ["tenant_id", "clave"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    _rls(_TABLA)


def downgrade() -> None:
    op.execute(f"drop policy if exists tenant_isolation on {_TABLA}")
    op.execute(f"alter table {_TABLA} no force row level security")
    op.execute(f"alter table {_TABLA} disable row level security")
    op.drop_index("uq_mensajes_guardados_tenant_clave", table_name=_TABLA)
    op.drop_table(_TABLA)
