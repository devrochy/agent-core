"""Razones de la guarda A2A y gasto por llamada (HU-AOS-18, adenda de ADR-0116).

Aditiva:

- `a2a_events.reason` pasa a tener CHECK con las razones de la guarda (`core/kernel/a2a.py`) más
  `no_ledger`, `timeout` y `error`. Entra `NOT VALID` y se valida en la misma revisión: hasta hoy
  nada escribe la tabla (las filas que hubiera tienen `reason` nulo y lo cumplen).
- Índice parcial `(tenant_id, a2a_call_id)` en `usage_ledger` para sumar el gasto de una llamada
  A2A sin recorrer el ledger del negocio. `CONCURRENTLY` no se usa: alembic corre la revisión en
  una transacción y el índice parcial solo cubre las filas `agent_run` (hoy ninguna).

Revision ID: 0106
Revises: 0105
Create Date: 2026-10-25
"""

from __future__ import annotations

from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None

RAZONES = (
    "turn_closed",
    "cycle",
    "no_link",
    "depth",
    "repeat",
    "calls",
    "owner_frozen",
    "data_class",
    "budget",
    "no_ledger",
    "timeout",
    "error",
)


def upgrade() -> None:
    lista = ", ".join(f"'{r}'" for r in RAZONES)
    op.execute(
        f"ALTER TABLE a2a_events ADD CONSTRAINT ck_a2a_events_reason "
        f"CHECK (reason IS NULL OR reason IN ({lista})) NOT VALID"
    )
    op.execute("ALTER TABLE a2a_events VALIDATE CONSTRAINT ck_a2a_events_reason")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_usage_ledger_tenant_a2a_call "
        "ON usage_ledger (tenant_id, a2a_call_id) WHERE a2a_call_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_usage_ledger_tenant_a2a_call")
    op.execute("ALTER TABLE a2a_events DROP CONSTRAINT IF EXISTS ck_a2a_events_reason")
