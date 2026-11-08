"""Conexión de WhatsApp vía Twilio (ADR-0186, PR-3).

Cuatro piezas, todas aditivas:

1. `twilio_connections`, tabla de NEGOCIO (invariante 1: `tenant_id`, RLS habilitada y forzada, policy de
   aislamiento, índice que empieza por `tenant_id`). Es 1:1 con su `channel_connections` (único parcial entre los
   vivos) y guarda lo propio de Twilio que no es secreto: `public_id` (aleatorio, va en la URL del webhook), los
   SID de la cuenta y del sender, el estado, el display name, las URL de webhook registradas (la firma se verifica
   contra ellas, no contra la URL que reconstruye el proxy) y los webhooks que el sender tenía antes. El Auth Token
   NO está aquí: va cifrado en `channel_connections.credentials_encrypted`. `subaccount_id` queda para la fase ISV
   (ADR-0187), sin FK porque su tabla todavía no existe.
2. Un número, un negocio: índice único parcial GLOBAL sobre `channel_connections (external_id)` de las conexiones
   `twilio` vivas, como hace `phone_numbers` con los números de Meta. La tabla conserva su RLS; la unicidad se
   aplica igual entre negocios. El borrado lógico libera el número.
3. `twilio_route_connection(public_id)`, SECURITY DEFINER: el webhook (PR-4) llega sin negocio y necesita saber de
   cuál es. Devuelve solo `(tenant_id, channel_connection_id)` de una conexión no borrada; nada más se lee sin el
   `tenant_scope`. No filtra `active`: el webhook lo comprueba ya dentro del negocio (una conexión pausada responde
   200 sin procesar, para que Twilio no reintente).
4. `twilio_account_en_otro_negocio(account_sid)`, SECURITY DEFINER (el negocio sale de `app.tenant_id`): una cuenta de Twilio, un negocio.
   El Auth Token de la cuenta firma los webhooks de todos sus números; si dos negocios compartieran la cuenta, uno
   podría firmar webhooks válidos para el `public_id` del otro y hacerse pasar por sus clientes. Devuelve solo un
   booleano. (Varias cuentas en un negocio, o varios números de una cuenta en el mismo negocio, sí.)

Sin DELETE para `app_runtime`: el borrado es lógico (invariante 8). `purge_tenant` descubre la tabla por su
`tenant_id` (0072) y la FK con ON DELETE CASCADE no le estorba.

Reversible: el downgrade elimina las funciones, el índice y la tabla (se pierden las conexiones de Twilio, que con la
bandera apagada no existen).

Revision ID: 0121
Revises: 0120
Create Date: 2026-11-07
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_UUID = UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_AHORA = sa.text("now()")
_TABLA = "twilio_connections"
_ESTADOS = "'pending','connected','degraded','revoked','error'"
# Como 0063/0073: las funciones SECURITY DEFINER corren con el rol que las crea. Sin BYPASSRLS, la RLS forzada de
# `twilio_connections` las limitaría al negocio en curso y «una cuenta, un negocio» fallaría abierta sin avisar.
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
            "0121: el rol de migraciones no es superusuario ni tiene BYPASSRLS. Las funciones SECURITY DEFINER "
            "de Twilio quedarían sujetas a la RLS forzada. Corre la migración con el dueño de la base."
        )
    op.create_table(
        _TABLA,
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("channel_connection_id", _UUID, nullable=False),
        sa.Column("public_id", sa.Text(), nullable=False),
        sa.Column("account_sid", sa.Text(), nullable=False),
        sa.Column("sender_sid", sa.Text(), nullable=False),
        sa.Column("sender_e164", sa.Text(), nullable=False),
        sa.Column("sender_status", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("webhook_inbound_url", sa.Text(), nullable=False),
        sa.Column("webhook_status_url", sa.Text(), nullable=False),
        sa.Column(
            "previous_webhook", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("subaccount_id", _UUID, nullable=True),
        sa.Column("last_health_at", _TS, nullable=True),
        sa.Column("last_error_code", sa.Integer(), nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("updated_at", _TS, nullable=False, server_default=_AHORA),
        # FK compuesta (patrón de 0067/0100): la fila no puede apuntar a la conexión de otro negocio.
        sa.ForeignKeyConstraint(
            ["tenant_id", "channel_connection_id"],
            ["channel_connections.tenant_id", "channel_connections.id"],
            name="fk_twilio_connections_conexion",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("public_id", name="uq_twilio_connections_public_id"),
        sa.CheckConstraint(f"status in ({_ESTADOS})", name="ck_twilio_connections_status"),
        sa.CheckConstraint(
            "account_sid ~ '^AC[0-9a-f]{32}$'", name="ck_twilio_connections_account_sid"
        ),
        sa.CheckConstraint(
            "sender_sid ~ '^XE[0-9a-f]{32}$'", name="ck_twilio_connections_sender_sid"
        ),
        sa.CheckConstraint(
            "sender_e164 ~ '^\\+[1-9][0-9]{6,14}$'", name="ck_twilio_connections_sender_e164"
        ),
        sa.CheckConstraint(
            "char_length(public_id) between 16 and 64", name="ck_twilio_connections_public_id"
        ),
    )
    op.create_index(
        "uq_twilio_connections_conexion_viva",
        _TABLA,
        ["tenant_id", "channel_connection_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    _rls(_TABLA)

    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_connections_twilio_sender_viva
        ON channel_connections (external_id)
        WHERE provider = 'twilio' AND deleted_at IS NULL
        """
    )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION twilio_route_connection(p_public_id text)
        RETURNS TABLE (tenant_id uuid, channel_connection_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            SELECT tc.tenant_id, tc.channel_connection_id
            FROM {_TABLA} tc
            JOIN channel_connections cc
              ON cc.id = tc.channel_connection_id AND cc.tenant_id = tc.tenant_id
            WHERE tc.public_id = p_public_id
              AND tc.deleted_at IS NULL
              AND cc.deleted_at IS NULL
              AND cc.provider = 'twilio'
            LIMIT 1
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION twilio_route_connection(text) FROM PUBLIC")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION twilio_account_en_otro_negocio(p_account_sid text)
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$
            -- El negocio es el del `tenant_scope` en curso, no un argumento. Sin negocio, falla cerrada (true).
            SELECT CASE
                WHEN nullif(current_setting('app.tenant_id', true), '') IS NULL THEN true
                ELSE EXISTS (
                    SELECT 1
                    FROM {_TABLA} tc
                    JOIN channel_connections cc
                      ON cc.id = tc.channel_connection_id AND cc.tenant_id = tc.tenant_id
                    WHERE tc.account_sid = p_account_sid
                      AND tc.tenant_id <> nullif(current_setting('app.tenant_id', true), '')::uuid
                      AND tc.deleted_at IS NULL
                      AND cc.deleted_at IS NULL
                )
            END
        $$;
        """
    )
    op.execute("REVOKE ALL ON FUNCTION twilio_account_en_otro_negocio(text) FROM PUBLIC")
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT EXECUTE ON FUNCTION twilio_route_connection(text) TO app_runtime;
                GRANT EXECUTE ON FUNCTION twilio_account_en_otro_negocio(text) TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS twilio_account_en_otro_negocio(text)")
    op.execute("DROP FUNCTION IF EXISTS twilio_route_connection(text)")
    op.execute("DROP INDEX IF EXISTS uq_channel_connections_twilio_sender_viva")
    op.execute(f"drop policy if exists tenant_isolation on {_TABLA}")
    op.execute(f"alter table {_TABLA} no force row level security")
    op.execute(f"alter table {_TABLA} disable row level security")
    op.drop_index("uq_twilio_connections_conexion_viva", table_name=_TABLA)
    op.drop_table(_TABLA)
