"""Modo Sandbox de WhatsApp vía Twilio, solo en test (HU-CONN-20, adenda del ADR-0186).

Todo aditivo sobre `twilio_connections` (la tabla ya tiene `tenant_id`, RLS forzada y su policy desde 0121):

1. `modo` (`sender|sandbox`, `DEFAULT 'sender'`): las filas que ya existen quedan como `sender`.
2. `sender_sid` admite NULL, con un CHECK que lo exige en `sender` y lo prohíbe en `sandbox` (el sandbox no tiene
   sender en la Senders API). El CHECK de formato de 0121 sigue igual: un NULL lo pasa.
3. `sandbox_join_code`: lo que sigue a `join` en el mensaje con el que un teléfono se une al sandbox de la cuenta. No
   es secreto (lo muestra la consola de Twilio). Solo existe en modo sandbox y se acota a minúsculas, números y
   guiones (Twilio genera dos palabras unidas por guion, p. ej. `cloud-forgotten`), hasta 64 caracteres.
4. `webhook_confirmado_at`: el primer entrante o estado con firma válida. En el sandbox las URL se pegan a mano en la
   consola y esa es la única prueba de que quedaron bien.
5. Índice único parcial `(tenant_id, account_sid)` de los sandbox vivos: un sandbox por cuenta en cada negocio, y el
   alta repetida encuentra la misma fila. Que la cuenta no esté en otro negocio lo sigue cuidando
   `twilio_account_en_otro_negocio` (0121), y el número compartido no choca en `uq_channel_connections_twilio_sender_viva`
   porque su `external_id` es `sandbox:<AccountSid>`.

Bajada: vuelve `sender_sid` a NOT NULL, así que falla si existe CUALQUIER fila sandbox, también una borrada
lógicamente (sigue con `sender_sid` NULL y no hay borrado físico, invariante 8). Corre con el rol de migraciones, que
salta la RLS: el conteo ve todos los negocios.

Revision ID: 0136
Revises: 0135
Create Date: 2026-11-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None

_TABLA = "twilio_connections"
_INDICE = "uq_twilio_connections_sandbox_cuenta_viva"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(_TABLA, sa.Column("modo", sa.Text(), nullable=False, server_default="sender"))
    op.add_column(_TABLA, sa.Column("sandbox_join_code", sa.Text(), nullable=True))
    op.add_column(
        _TABLA, sa.Column("webhook_confirmado_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.alter_column(_TABLA, "sender_sid", nullable=True)
    op.create_check_constraint(
        "ck_twilio_connections_modo", _TABLA, "modo in ('sender', 'sandbox')"
    )
    op.create_check_constraint(
        "ck_twilio_connections_sender_sid_por_modo",
        _TABLA,
        "(modo = 'sender' and sender_sid is not null) or (modo = 'sandbox' and sender_sid is null)",
    )
    op.create_check_constraint(
        "ck_twilio_connections_sandbox_join_code",
        _TABLA,
        "sandbox_join_code is null or (modo = 'sandbox' "
        "and sandbox_join_code ~ '^[a-z0-9]+(-[a-z0-9]+)*$' and char_length(sandbox_join_code) <= 64)",
    )
    op.create_index(
        _INDICE,
        _TABLA,
        ["tenant_id", "account_sid"],
        unique=True,
        postgresql_where=sa.text("modo = 'sandbox' AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    sandbox = (
        op.get_bind()
        .execute(sa.text(f"select count(*) from {_TABLA} where modo = 'sandbox'"))
        .scalar()
    )
    if sandbox:
        raise RuntimeError(
            f"0136: hay {sandbox} conexiones sandbox de Twilio (vivas o borradas). Sin sender_sid no caben en el "
            "esquema de 0135 y no hay borrado físico: no se puede bajar."
        )
    op.drop_index(_INDICE, table_name=_TABLA)
    op.drop_constraint("ck_twilio_connections_sandbox_join_code", _TABLA, type_="check")
    op.drop_constraint("ck_twilio_connections_sender_sid_por_modo", _TABLA, type_="check")
    op.drop_constraint("ck_twilio_connections_modo", _TABLA, type_="check")
    op.alter_column(_TABLA, "sender_sid", nullable=False)
    op.drop_column(_TABLA, "webhook_confirmado_at")
    op.drop_column(_TABLA, "sandbox_join_code")
    op.drop_column(_TABLA, "modo")
