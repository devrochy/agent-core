"""Renombra el plugin_id de Google Calendar al migrarlo a pack-calendar (C4 #2).

El pack vive ahora en packages/pack-calendar con entry_point pack_calendar.
Sin migraciones propias ni config persistida; se alinea el registro y el
enablement `agent_specs.spec.packs` (mismo patrón que la 0007).

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-28
"""
from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _rename_packs(bind, old: str, new: str) -> None:
    for spec_id, spec in bind.execute(sa.text("select id, spec from agent_specs")).fetchall():
        spec = dict(spec)
        packs = spec.get("packs", []) or []
        if old in packs:
            spec["packs"] = [new if p == old else p for p in packs]
            bind.execute(
                sa.text("update agent_specs set spec = :spec where id = :id"),
                {"spec": json.dumps(spec), "id": spec_id},
            )


def upgrade() -> None:
    bind = op.get_bind()
    # FK plugin_configs->plugins: se dropea y re-crea por si hubiera config.
    bind.execute(
        sa.text("alter table plugin_configs drop constraint if exists plugin_configs_plugin_id_fkey")
    )
    bind.execute(
        sa.text("update plugins set id = 'pack_calendar' where id = 'mod_google_calendar'")
    )
    bind.execute(
        sa.text(
            "update plugin_configs set plugin_id = 'pack_calendar' "
            "where plugin_id = 'mod_google_calendar'"
        )
    )
    bind.execute(
        sa.text(
            "alter table plugin_configs add constraint plugin_configs_plugin_id_fkey "
            "foreign key (plugin_id) references plugins(id)"
        )
    )
    # enablement (ADR-0019): un spec con el pack viejo no debe quedar huérfano.
    _rename_packs(bind, "mod_google_calendar", "pack_calendar")
    # el pack no tiene árbol alembic propio; si existiera una tabla de versiones
    # residual, se renombra por higiene.
    old_vt = bind.execute(sa.text("select to_regclass('alembic_version_mod_google_calendar')")).scalar()
    new_vt = bind.execute(sa.text("select to_regclass('alembic_version_pack_calendar')")).scalar()
    if old_vt and not new_vt:
        bind.execute(
            sa.text("alter table alembic_version_mod_google_calendar rename to alembic_version_pack_calendar")
        )


def downgrade() -> None:
    bind = op.get_bind()
    old_vt = bind.execute(sa.text("select to_regclass('alembic_version_pack_calendar')")).scalar()
    if old_vt:
        bind.execute(
            sa.text("alter table alembic_version_pack_calendar rename to alembic_version_mod_google_calendar")
        )
    _rename_packs(bind, "pack_calendar", "mod_google_calendar")
    bind.execute(
        sa.text("alter table plugin_configs drop constraint if exists plugin_configs_plugin_id_fkey")
    )
    bind.execute(
        sa.text("update plugin_configs set plugin_id = 'mod_google_calendar' where plugin_id = 'pack_calendar'")
    )
    bind.execute(
        sa.text("update plugins set id = 'mod_google_calendar' where id = 'pack_calendar'")
    )
    bind.execute(
        sa.text(
            "alter table plugin_configs add constraint plugin_configs_plugin_id_fkey "
            "foreign key (plugin_id) references plugins(id)"
        )
    )