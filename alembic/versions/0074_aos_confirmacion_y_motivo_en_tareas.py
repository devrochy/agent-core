"""Agent OS · estado de la conversación: confirmación pendiente y motivo en la tarea (HU-AOS-04).

`conversation_tasks` es la proyección de `task_events` (ADR-0114 §1). Le faltaban dos datos que
el turno necesita leer sin recorrer los eventos (ADR-0114 descarta "eventos sin proyección"):

- `pending_confirmation jsonb`: lo que la etapa espera que el cliente confirme
  (`{"kind": "stage"|"slot"|"action", "name": …, "seq": n}`), escrito por el evento
  `confirmation_requested` y limpiado por `confirmed` (o al cambiar de etapa o cerrar la tarea).
  La confirmación de un procedimiento vive aquí y no en `conversations.state.pending_confirmation`,
  que queda solo para acciones fuera de procedimiento (ADR-0114 §1);
- `status_reason text`: el motivo de un cierre (`episode_expired`, `release_changed`, …), como
  código, nunca texto libre. El texto de una persona va en el `data` del evento.

Ambas se reconstruyen desde los eventos como el resto de la proyección (`core/kernel/state.py`).

Además, CHECKs con las reglas que aplica el reductor, para que una proyección mal escrita por otro
camino no pase: la forma de `pending_confirmation`, que solo existe con la tarea abierta; el motivo
solo en estados terminales y obligatorio en `cancelled`/`failed`/`escalated`; `completed_at` presente
si y solo si la tarea está cerrada.

Sin riesgo de bloqueo: columnas nulas sin default (solo catálogo) y CHECKs sobre una tabla que nadie
escribe todavía (el runtime empieza a escribirla con esta HU, detrás de `aos_procedures`). Por eso
los CHECKs se validan en la misma transacción: con la tabla vacía el VALIDATE es instantáneo.

Revision ID: 0074
Revises: 0073
Create Date: 2026-10-10
"""

from __future__ import annotations

from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


_TERMINALES = "('completed','cancelled','failed','escalated')"
_CHECKS = {
    "ck_conversation_tasks_pending_confirmation": (
        "pending_confirmation IS NULL OR ("
        "jsonb_typeof(pending_confirmation) = 'object' "
        "AND pending_confirmation->>'kind' IN ('stage','slot','action') "
        "AND jsonb_typeof(pending_confirmation->'name') = 'string' "
        "AND jsonb_typeof(pending_confirmation->'seq') = 'number' "
        "AND status IN ('active','suspended'))"
    ),
    "ck_conversation_tasks_status_reason": (
        f"(status_reason IS NULL OR (status_reason ~ '^[a-z_]{{1,40}}$' AND status IN {_TERMINALES})) "
        "AND (status NOT IN ('cancelled','failed','escalated') OR status_reason IS NOT NULL)"
    ),
    "ck_conversation_tasks_completed_at": f"(status IN {_TERMINALES}) = (completed_at IS NOT NULL)",
}


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE conversation_tasks
          ADD COLUMN pending_confirmation jsonb,
          ADD COLUMN status_reason        text
        """
    )
    for nombre, condicion in _CHECKS.items():
        op.execute(f"ALTER TABLE conversation_tasks ADD CONSTRAINT {nombre} CHECK ({condicion}) NOT VALID")
        op.execute(f"ALTER TABLE conversation_tasks VALIDATE CONSTRAINT {nombre}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE conversation_tasks
          DROP CONSTRAINT IF EXISTS ck_conversation_tasks_completed_at,
          DROP CONSTRAINT IF EXISTS ck_conversation_tasks_status_reason,
          DROP CONSTRAINT IF EXISTS ck_conversation_tasks_pending_confirmation,
          DROP COLUMN IF EXISTS status_reason,
          DROP COLUMN IF EXISTS pending_confirmation
        """
    )
