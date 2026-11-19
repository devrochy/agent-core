"""El botón que pide el teléfono en la cola de salida (HU-CONN-26, ADR-0199 §10).

`messages.interactive` admite un tercer `type`, `contacto` (`{"type": "contacto", "body", "button"}`): la tool del core
`pedir_telefono` lo encola y el adaptador de Telegram lo manda como teclado de respuesta con `request_contact`. El CHECK
`ck_messages_interactive_objeto` de la 0082 solo aceptaba `button` y `list`, así que se reemplaza por uno igual con el
tipo nuevo. Las demás condiciones (objeto, `body` de texto, solo outbound, nunca con plantilla) no cambian.

Riesgo de bloqueo: igual que la 0082. Quitar y volver a poner el CHECK `NOT VALID` solo toca el catálogo (ACCESS
EXCLUSIVE de milisegundos, con `lock_timeout` de 5 s). La validación va en su propia transacción con
`autocommit_block`: VALIDATE toma SHARE UPDATE EXCLUSIVE, que no bloquea lecturas ni escrituras. Re-ejecutable.

Downgrade: revertir el código antes (si no, la tool seguiría encolando `contacto`). Vuelve el CHECK de la 0082 `NOT
VALID`, sin validar: las filas `contacto` que ya existan quedan como están (validarlo fallaría con ellas).

Revision ID: 0139
Revises: 0138
Create Date: 2026-11-16
"""

from __future__ import annotations

from alembic import op

revision = "0139"
down_revision = "0138"
branch_labels = None
depends_on = None

_CHECK = "ck_messages_interactive_objeto"


def _condicion(tipos: str) -> str:
    return (
        "interactive IS NULL OR ("
        "jsonb_typeof(interactive) = 'object' "
        f"AND interactive->>'type' IN ({tipos}) "
        "AND jsonb_typeof(interactive->'body') = 'string' "
        "AND direction = 'outbound' "
        "AND template_name IS NULL)"
    )


_NUEVA = _condicion("'button','list','contacto'")
_ANTERIOR = _condicion("'button','list'")


def _reemplazar(condicion: str) -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"ALTER TABLE messages DROP CONSTRAINT IF EXISTS {_CHECK}, "
        f"ADD CONSTRAINT {_CHECK} CHECK ({condicion}) NOT VALID"
    )


def upgrade() -> None:
    _reemplazar(_NUEVA)
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = 0")
        op.execute(f"ALTER TABLE messages VALIDATE CONSTRAINT {_CHECK}")
        op.execute("RESET statement_timeout")
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    """Revertir el código ANTES. El CHECK de la 0082 vuelve sin validar: no se borran ni cambian filas `contacto`."""
    _reemplazar(_ANTERIOR)
