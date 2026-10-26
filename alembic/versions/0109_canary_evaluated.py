"""Agent OS · `canary_evaluated` en los eventos de release (HU-AOS-32, adenda de ADR-0117).

El veredicto del canary (`listo` o `peor`) se decide una sola vez, al juntar la muestra, y queda en
un evento propio: no se reutilizan `evaluated` (la medición del gate, que lee el corredor) ni
`approved`/`rejected` (los veredictos de pass^k que leen el gate y el tablero). No entra en el ciclo
de vida (`promocion.CICLO_DE_VIDA`).

Aditiva: se reemplaza el CHECK por uno con el tipo nuevo. Dentro de la transacción de Alembic el
`ADD CONSTRAINT` toma un bloqueo exclusivo hasta el commit; la tabla es chica y es inocuo.

Revision ID: 0109
Revises: 0108
Create Date: 2026-10-26
"""

from __future__ import annotations

from alembic import op

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None

_ANTES = (
    "'built', 'evaluated', 'approved', 'rejected', 'promoted', 'canary_started', 'canary_stopped', "
    "'rolled_back', 'retired', 'invalid'"
)
_AHORA = _ANTES + ", 'canary_evaluated'"


def _check(tipos: str, *, validar: bool = True) -> None:
    op.execute("ALTER TABLE release_events DROP CONSTRAINT IF EXISTS release_events_type_check")
    op.execute(
        f"ALTER TABLE release_events ADD CONSTRAINT release_events_type_check CHECK (type IN ({tipos})) NOT VALID"
    )
    if validar:
        op.execute("ALTER TABLE release_events VALIDATE CONSTRAINT release_events_type_check")


def upgrade() -> None:
    _check(_AHORA)


def downgrade() -> None:
    # Sin borrado físico (invariante 8): el CHECK anterior vuelve como NOT VALID, así los eventos
    # `canary_evaluated` que ya existen se conservan y las filas nuevas se validan con la regla vieja.
    _check(_ANTES, validar=False)
