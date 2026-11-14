"""Limpia la identidad de sesiones `waweb` ya deslogueadas (ADR-0074).

La migración 0130 hizo backfill de `jid`/`phone`/`push_name` sobre todas las
filas, incluidas las que ya estaban en `logged_out`. El panel podía mostrarlas
como vinculadas y el conector alertaba «número en más de un negocio» en cada
reinicio por una sesión que ya no tiene credenciales.

Se limpia solo la identidad visible; `last_connected_at` se conserva como
historial (`linked_before`) y `creds_encrypted` ya estaba vacío por el logout.

Revision ID: 0132
Revises: 0131
Create Date: 2026-11-14
"""

from __future__ import annotations

from alembic import op

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None

_TABLE = "waweb_sessions"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"""
        UPDATE {_TABLE}
        SET jid = NULL,
            phone = NULL,
            push_name = NULL,
            business_name = NULL,
            platform = NULL,
            linked_at = NULL
        WHERE deleted_at IS NULL
          AND status = 'logged_out'
          AND (
            jid IS NOT NULL
            OR phone IS NOT NULL
            OR push_name IS NOT NULL
            OR business_name IS NOT NULL
            OR platform IS NOT NULL
            OR linked_at IS NOT NULL
          )
        """
    )


def downgrade() -> None:
    # Sin rollback: los datos de identidad de una sesión deslogueada no se
    # pueden reconstruir y no deben volver al panel.
    pass
