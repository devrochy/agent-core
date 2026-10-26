"""Agent OS · la toma humana en la tarea: `taken` y `released` en `task_events` (HU-AOS-28, ADR-0169 §5).

Cuando una persona toma una conversación escalada queda un evento `taken` (actor `human`) en cada tarea
abierta, y `released` al devolverla al agente o resolverla. Son anotaciones: no cambian la proyección.

Se reemplaza el CHECK inline de `task_events.type` (Postgres lo nombró `task_events_type_check` en la
0069) por uno con nombre, `ck_task_events_type`, que entra `NOT VALID` y se valida en la misma
revisión (como la 0106). Aditiva.

El downgrade vuelve al CHECK anterior como `NOT VALID` sin validarlo: con filas `taken`/`released`
el CHECK viejo no valida y `task_events` es append-only (`prevent_mutation()`), así que en la práctica
la migración no se revierte sin perder esas filas, que no se borran (invariante 8).

Revision ID: 0111
Revises: 0110
Create Date: 2026-10-26
"""

from __future__ import annotations

from alembic import op

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None

_ANTES = (
    "started", "slot_set", "slot_cleared", "stage_entered", "precondition_failed", "action_requested",
    "action_succeeded", "action_failed", "confirmation_requested", "confirmed", "suspended", "resumed",
    "owner_changed", "revalidated", "episode_expired", "completed", "cancelled", "failed", "escalated",
)
_AHORA = (*_ANTES, "taken", "released")


def _lista(tipos: tuple[str, ...]) -> str:
    return ", ".join(f"'{t}'" for t in tipos)


def upgrade() -> None:
    op.execute("ALTER TABLE task_events DROP CONSTRAINT IF EXISTS task_events_type_check")
    op.execute("ALTER TABLE task_events DROP CONSTRAINT IF EXISTS ck_task_events_type")
    op.execute(f"ALTER TABLE task_events ADD CONSTRAINT ck_task_events_type CHECK (type IN ({_lista(_AHORA)})) NOT VALID")
    op.execute("ALTER TABLE task_events VALIDATE CONSTRAINT ck_task_events_type")


def downgrade() -> None:
    op.execute("ALTER TABLE task_events DROP CONSTRAINT IF EXISTS ck_task_events_type")
    op.execute(
        f"ALTER TABLE task_events ADD CONSTRAINT task_events_type_check CHECK (type IN ({_lista(_ANTES)})) NOT VALID"
    )
