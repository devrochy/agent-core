"""Los crons de leads pasan al core (ADR-0193 §3, HU-AOS-70, parte 2).

`pack_commerce.follow_up_leads` y `pack_commerce.recordar_seguimientos` ahora son `core.leads_follow_up` y
`core.leads_recordar_seguimientos`. La política que cada negocio guardó para ellos en `tenant_cron_configs` (0142:
encendido, hora local, ventana) se renombra para no perderla. Solo datos, sin cambio de esquema; el historial de
`cron_runs` queda con el nombre viejo (es historia).

Reversible: el downgrade devuelve los nombres viejos.

Revision ID: 0147
Revises: 0146
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0147"
down_revision = "0146"
branch_labels = None
depends_on = None

_RENOMBRES = (
    ("pack_commerce.follow_up_leads", "core.leads_follow_up"),
    ("pack_commerce.recordar_seguimientos", "core.leads_recordar_seguimientos"),
)


# `tenant_cron_configs` tiene RLS forzada (0142): sin BYPASSRLS el UPDATE tocaría 0 filas en silencio.
_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"


def _renombrar(pares: tuple[tuple[str, str], ...]) -> None:
    # Si el código nuevo ya escribió la fila con el nombre nuevo (arrancó antes de migrar), gana esa y la vieja queda.
    for viejo, nuevo in pares:
        op.execute(
            sa.text(
                "update tenant_cron_configs v set cron_name = :nuevo where v.cron_name = :viejo and not exists ("
                "select 1 from tenant_cron_configs n where n.tenant_id = v.tenant_id and n.cron_name = :nuevo)"
            ).bindparams(viejo=viejo, nuevo=nuevo)
        )


def upgrade() -> None:
    if not op.get_bind().execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError("0147: el rol de migraciones no puede saltar la RLS de tenant_cron_configs; usa el dueño.")
    _renombrar(_RENOMBRES)


def downgrade() -> None:
    _renombrar(tuple((nuevo, viejo) for viejo, nuevo in _RENOMBRES))
