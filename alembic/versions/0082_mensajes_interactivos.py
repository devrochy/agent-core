"""Mensajes interactivos de WhatsApp en la cola de salida (HU-AOS-09).

`messages.interactive jsonb`: el objeto `interactive` de Graph sin el destinatario
(`{"type": "button", "body", "buttons": [{"id", "title"}]}`) de un outbound con botones de
respuesta. Lo escribe `sender.send_interactive_to_conversation` (la confirmación con botón que
arma el kernel) y lo lee `outbound_send._send_one`, que lo envía en orden con el resto de la cola.
Es genérico a propósito: la salida interactiva de HU-INMO-03 (listas) usa la misma columna.

`content` sigue guardando el cuerpo para el panel. Sin columna, las alternativas eran peores:
enviar fuera de la cola rompe el orden y la idempotencia de `outbound_send`, y reusar
`template_components` sin `template_name` hace que la fila mienta.

Riesgo de bloqueo: `messages` es grande, pero agregar una columna nula sin default solo toca el
catálogo (ACCESS EXCLUSIVE de milisegundos, con `lock_timeout` de 5 s para no hacer cola detrás de
una transacción larga). El CHECK se agrega `NOT VALID` (sin recorrer la tabla) y se valida en su
propia transacción con `autocommit_block`: VALIDATE toma SHARE UPDATE EXCLUSIVE, que no bloquea
lecturas ni escrituras. Re-ejecutable. El CHECK exige la forma mínima (`type`, `body`) y que solo
la lleve un outbound que no es plantilla.

Downgrade: revertir el código antes (el modelo mapea la columna).

Revision ID: 0082
Revises: 0080
Create Date: 2026-10-11
"""

from __future__ import annotations

from alembic import op

revision = "0082"
down_revision = "0080"
branch_labels = None
depends_on = None

_CHECK = "ck_messages_interactive_objeto"
# La forma mínima y la coherencia con las demás columnas: solo un outbound, nunca a la vez que una
# plantilla, con `type` y `body`. Todas las filas existentes tienen `interactive` NULL.
_CONDICION = (
    "interactive IS NULL OR ("
    "jsonb_typeof(interactive) = 'object' "
    "AND interactive->>'type' IN ('button','list') "
    "AND jsonb_typeof(interactive->'body') = 'string' "
    "AND direction = 'outbound' "
    "AND template_name IS NULL)"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS interactive jsonb")
    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = '{_CHECK}' AND conrelid = 'public.messages'::regclass
          ) THEN
            ALTER TABLE messages ADD CONSTRAINT {_CHECK} CHECK ({_CONDICION}) NOT VALID;
          END IF;
        END $$
        """
    )
    with op.get_context().autocommit_block():
        # VALIDATE recorre la tabla con SHARE UPDATE EXCLUSIVE (no bloquea DML): sin tope de
        # sentencia, y los dos parámetros vuelven a su valor para las migraciones siguientes.
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = 0")
        op.execute(f"ALTER TABLE messages VALIDATE CONSTRAINT {_CHECK}")
        op.execute("RESET statement_timeout")
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    """Revertir el código ANTES de bajar la migración (el modelo `Message` mapea la columna). Se
    pierden los botones de los interactivos que sigan en cola: saldrían como texto del cuerpo."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"ALTER TABLE messages DROP CONSTRAINT IF EXISTS {_CHECK}, DROP COLUMN IF EXISTS interactive")
