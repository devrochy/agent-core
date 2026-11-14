"""Identidad visible y fecha de vinculación de `waweb` (ADR-0074).

El panel mostraba `numero: User` porque `waweb_sessions.jid` guardaba el
`str()` del proto `JID` (`User: "573..."`). Esta migración agrega la identidad
que Neonize entrega al vincular (`PairStatusEv`/`Device`) y normaliza los JID
viejos para que el número se vea de inmediato.

- `phone`: solo dígitos, sin `+` ni `@server`.
- `push_name`, `business_name`, `platform`: nombre de perfil, nombre del
  negocio y tipo de dispositivo.
- `linked_at`: primera vinculación. Se conserva al reconectar; se limpia al
  re-vincular o cerrar sesión.

Backfill:
- `jid` con el formato viejo `User: "..."` → `<digitos>@s.whatsapp.net`.
- `phone` desde el JID normalizado (solo números `@s.whatsapp.net`).
- `linked_at` desde `last_connected_at` (mejor aproximación: la fecha exacta de
  la primera vinculación no se guardaba antes).

**Rollback:** elimina las columnas; los JID normalizados no se revierten (no hay
forma de reconstruir el `User: "..."` original y el valor normalizado es el
correcto).

Revision ID: 0130
Revises: 0129
Create Date: 2026-11-14
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None

_TABLE = "waweb_sessions"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.add_column(_TABLE, sa.Column("phone", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("push_name", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("business_name", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("platform", sa.Text(), nullable=True))
    op.add_column(_TABLE, sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True))

    # JID viejo: `User: "573005550103"` (o con server). Se normaliza al formato
    # que usa el resto del sistema y se aprovecha para llenar `phone`.
    op.execute(
        f"""
        UPDATE {_TABLE}
        SET phone = substring(jid from '([0-9]{{6,15}})'),
            jid = substring(jid from '([0-9]{{6,15}})') || '@s.whatsapp.net'
        WHERE deleted_at IS NULL
          AND jid LIKE 'User:%'
          AND substring(jid from '([0-9]{{6,15}})') IS NOT NULL
        """
    )
    # JID ya válido pero sin `phone` (sesiones creadas antes de esta columna).
    op.execute(
        f"""
        UPDATE {_TABLE}
        SET phone = split_part(jid, '@', 1)
        WHERE deleted_at IS NULL
          AND phone IS NULL
          AND jid ~ '^[0-9]{{6,15}}@s\\.whatsapp\\.net$'
        """
    )
    # Un JID viejo que no tenía dígitos resolubles no debe seguir mostrando "User".
    op.execute(
        f"""
        UPDATE {_TABLE}
        SET jid = NULL
        WHERE deleted_at IS NULL
          AND jid LIKE 'User:%'
        """
    )
    # Primera vinculación: la fecha exacta no existía; `last_connected_at` es la
    # mejor aproximación disponible y se conserva como historial.
    op.execute(
        f"""
        UPDATE {_TABLE}
        SET linked_at = last_connected_at
        WHERE deleted_at IS NULL
          AND linked_at IS NULL
          AND last_connected_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "linked_at")
    op.drop_column(_TABLE, "platform")
    op.drop_column(_TABLE, "business_name")
    op.drop_column(_TABLE, "push_name")
    op.drop_column(_TABLE, "phone")
