"""Conexión de Telegram con bot propio por negocio (ADR-0199, PR core 1).

Tres piezas, todas aditivas, copiadas de 0121 (WhatsApp vía Twilio):

1. `telegram_connections`, tabla de NEGOCIO (invariante 1: `tenant_id`, RLS habilitada y forzada, policy de
   aislamiento, índice que empieza por `tenant_id`). Es 1:1 con su `channel_connections` (único parcial entre los
   vivos) y guarda lo propio del bot que no es secreto: `public_id` (aleatorio, va en la URL del webhook), el
   `@usuario` y el nombre del bot, el estado, la URL de webhook registrada y lo último que dijo `getWebhookInfo`. El
   token del bot y el secreto del webhook NO están aquí: van cifrados en `channel_connections.credentials_encrypted`.
   El `bot_id` tampoco: es `channel_connections.external_id` (lo da `getMe`, no el token), la única fuente de verdad.
2. Un bot, un negocio: índice único parcial GLOBAL sobre `channel_connections (external_id)` de las conexiones
   `telegram` vivas. Un bot solo admite un webhook, así que dos negocios con el mismo bot se pisarían los mensajes. La
   tabla conserva su RLS; la unicidad se aplica igual entre negocios y el alta traduce el choque a 409
   `bot_en_otro_negocio` sin decir cuál. El borrado lógico libera el bot. A diferencia de Twilio (0121, punto 4), no
   hace falta una función que mire otros negocios: en Twilio la cuenta y el número son cosas distintas; aquí el bot
   ES la clave del índice.
3. `telegram_route_connection(public_id)`, SECURITY DEFINER: el webhook llega sin negocio y necesita saber de cuál
   es. Devuelve solo `(tenant_id, channel_connection_id)` de una conexión no borrada; nada más se lee sin el
   `tenant_scope`. No filtra `active`: el webhook lo comprueba ya dentro del negocio.

Sin DELETE para `app_runtime`: el borrado es lógico (invariante 8). `purge_tenant` descubre la tabla por su
`tenant_id` (0072) y la FK con ON DELETE CASCADE no le estorba.

Reversible: el downgrade elimina la función, el índice y la tabla (se pierden las conexiones de Telegram, que con el
tipo oculto hasta el PR core 4 no existen).

Revision ID: 0137
Revises: 0136
Create Date: 2026-11-15
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_UUID = UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_AHORA = sa.text("now()")
_TABLA = "telegram_connections"
_ESTADOS = "'pending','connected','degraded','revoked','error'"
# Como 0121: la función SECURITY DEFINER corre con el rol que la crea. Sin BYPASSRLS, la RLS forzada de
# `telegram_connections` la limitaría al negocio en curso y el webhook nunca encontraría su conexión.
_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    op.execute(f"revoke all on {table} from app_runtime")
    op.execute(f"grant select, insert, update on {table} to app_runtime")


def upgrade() -> None:
    if not op.get_bind().execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError(
            "0137: el rol de migraciones no es superusuario ni tiene BYPASSRLS. La función SECURITY DEFINER "
            "de Telegram quedaría sujeta a la RLS forzada. Corre la migración con el dueño de la base."
        )
    op.create_table(
        _TABLA,
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("channel_connection_id", _UUID, nullable=False),
        sa.Column("public_id", sa.Text(), nullable=False),
        sa.Column("bot_username", sa.Text(), nullable=False, server_default=""),
        sa.Column("bot_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("webhook_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("webhook_confirmado_at", _TS, nullable=True),
        sa.Column("pending_update_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("last_error_at", _TS, nullable=True),
        sa.Column("last_update_at", _TS, nullable=True),
        sa.Column("last_checked_at", _TS, nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("updated_at", _TS, nullable=False, server_default=_AHORA),
        # FK compuesta (patrón de 0067/0100/0121): la fila no puede apuntar a la conexión de otro negocio.
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_connection_id"],
            ["channel_connections.tenant_id", "channel_connections.id"],
            name="fk_telegram_connections_conexion",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("public_id", name="uq_telegram_connections_public_id"),
        sa.CheckConstraint(f"status in ({_ESTADOS})", name="ck_telegram_connections_status"),
        sa.CheckConstraint(
            "char_length(public_id) between 16 and 64", name="ck_telegram_connections_public_id"
        ),
        sa.CheckConstraint(
            "pending_update_count >= 0", name="ck_telegram_connections_pending_update_count"
        ),
    )
    op.create_index(
        "uq_telegram_connections_conexion_viva",
        _TABLA,
        ["tenant_id", "channel_connection_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    _rls(_TABLA)

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_connections_telegram_bot_viva
        ON channel_connections (external_id)
        WHERE provider = 'telegram' AND deleted_at IS NULL
        """
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION telegram_route_connection(p_public_id text)
        RETURNS TABLE (tenant_id uuid, channel_connection_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT tc.tenant_id, tc.channel_connection_id
            FROM {_TABLA} tc
            JOIN channel_connections cc
              ON cc.id = tc.channel_connection_id AND cc.tenant_id = tc.tenant_id
            WHERE tc.public_id = p_public_id
              AND tc.deleted_at IS NULL
              AND cc.deleted_at IS NULL
              AND cc.provider = 'telegram'
            LIMIT 1
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION telegram_route_connection(text) FROM PUBLIC")
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT EXECUTE ON FUNCTION telegram_route_connection(text) TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS telegram_route_connection(text)")
    op.execute("DROP INDEX IF EXISTS uq_channel_connections_telegram_bot_viva")
    op.execute(f"drop policy if exists tenant_isolation on {_TABLA}")
    op.execute(f"alter table {_TABLA} no force row level security")
    op.execute(f"alter table {_TABLA} disable row level security")
    op.drop_index("uq_telegram_connections_conexion_viva", table_name=_TABLA)
    op.drop_table(_TABLA)
