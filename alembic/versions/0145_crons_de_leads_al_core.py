"""Los crons de leads pasan al core (ADR-0193 §3, HU-AOS-70, parte 2).

`pack_commerce.follow_up_leads` y `pack_commerce.recordar_seguimientos` ahora son `core.leads_follow_up` y
`core.leads_recordar_seguimientos`. La política que cada negocio guardó para ellos en `tenant_cron_configs` (0142:
encendido, hora local, ventana) se renombra para no perderla. Solo datos, sin cambio de esquema; el historial de
`cron_runs` queda con el nombre viejo (es historia).

Reversible: el downgrade devuelve los nombres viejos.

Revision ID: 0145
Revises: 0144
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None

_RENOMBRES = (
    ("pack_commerce.follow_up_leads", "core.leads_follow_up"),
    ("pack_commerce.recordar_seguimientos", "core.leads_recordar_seguimientos"),
)


def _renombrar(pares: tuple[tuple[str, str], ...]) -> None:
    for viejo, nuevo in pares:
        op.execute(
            sa.text("update tenant_cron_configs set cron_name = :nuevo where cron_name = :viejo").bindparams(
                viejo=viejo, nuevo=nuevo
            )
        )


def upgrade() -> None:
    _renombrar(_RENOMBRES)


def downgrade() -> None:
    _renombrar(tuple((nuevo, viejo) for viejo, nuevo in _RENOMBRES))
