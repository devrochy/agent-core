"""Pausa por agente (HU-ARCH-38, ADR-0200): columnas vivas en `agents` y la razón `agent_paused` de A2A.

- `agents.paused_at`, `paused_by`, `paused_reason`: estado VIVO, fuera de la release congelada (la release
  copia `status` y `agent_instances.enabled`, por eso no sirven). Se lee al asignar el turno: un especialista
  pausado no atiende y el turno pasa al agente por defecto. `agents` ya tiene RLS forzada (0069).
- `a2a_events.reason` suma `agent_paused`: la guarda A2A niega el handoff y la consulta hacia un agente pausado.

Aditiva: columnas nullable sin default (solo metadatos, sin reescritura). El CHECK se recrea `NOT VALID` y se
valida en la misma revisión (las filas existentes ya lo cumplen).

Revision ID: 0141
Revises: 0140
Create Date: 2026-11-16
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0141"
down_revision = "0140"
branch_labels = None
depends_on = None

_RAZONES_0106 = (
    "turn_closed", "cycle", "no_link", "depth", "repeat", "calls", "owner_frozen", "data_class", "budget",
    "no_ledger", "timeout", "error",
)


def _check(razones: tuple[str, ...], *, validar: bool = True) -> None:
    lista = ", ".join(f"'{r}'" for r in razones)
    op.execute("ALTER TABLE a2a_events DROP CONSTRAINT IF EXISTS ck_a2a_events_reason")
    op.execute(
        f"ALTER TABLE a2a_events ADD CONSTRAINT ck_a2a_events_reason "
        f"CHECK (reason IS NULL OR reason IN ({lista})) NOT VALID"
    )
    if validar:
        op.execute("ALTER TABLE a2a_events VALIDATE CONSTRAINT ck_a2a_events_reason")


def upgrade() -> None:
    op.add_column("agents", sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agents", sa.Column("paused_by", sa.Uuid(), nullable=True))
    op.add_column("agents", sa.Column("paused_reason", sa.Text(), nullable=True))
    _check((*_RAZONES_0106, "agent_paused"))


def downgrade() -> None:
    # Sin VALIDATE: puede haber filas con `agent_paused` (mismo criterio que 0111).
    _check(_RAZONES_0106, validar=False)
    op.drop_column("agents", "paused_reason")
    op.drop_column("agents", "paused_by")
    op.drop_column("agents", "paused_at")
