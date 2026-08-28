"""Renombra el plugin_id del CRM al extraerlo como pack_commerce (Sprint 4, ADR-0020).

- `plugins.id` y `plugin_configs.plugin_id`: mod_crm_engine → pack_commerce.
- `agent_specs.spec.packs`: mod_crm_engine → pack_commerce (enablement, ADR-0019).
- `alembic_version_mod_crm_engine` → `alembic_version_pack_commerce` (para que el
  árbol del pack no se re-ejecute desde crm_0001).
"""
from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # registro del plugin + config cifrada de CULTO (no perder el plugin_configs).
    # La FK plugin_configs->plugins impide el rename en orden; se dropea y re-crea.
    bind.execute(sa.text("alter table plugin_configs drop constraint if exists plugin_configs_plugin_id_fkey"))
    bind.execute(
        sa.text("update plugins set id = 'pack_commerce' where id = 'mod_crm_engine'")
    )
    bind.execute(
        sa.text("update plugin_configs set plugin_id = 'pack_commerce' where plugin_id = 'mod_crm_engine'")
    )
    bind.execute(
        sa.text(
            "alter table plugin_configs add constraint plugin_configs_plugin_id_fkey "
            "foreign key (plugin_id) references plugins(id)"
        )
    )
    # enablement en los specs de los tenants
    for spec_id, spec in bind.execute(
        sa.text("select id, spec from agent_specs")
    ).fetchall():
        spec = dict(spec)
        if "mod_crm_engine" in (spec.get("packs", []) or []):
            spec["packs"] = ["pack_commerce" if p == "mod_crm_engine" else p for p in spec.get("packs", [])]
            bind.execute(
                sa.text("update agent_specs set spec = :spec where id = :id"),
                {"spec": json.dumps(spec), "id": spec_id},
            )
    # tabla de versiones del árbol alembic del pack: si existe la vieja y NO la
    # nueva, se renombra para conservar el historial crm_0001/crm_0002 y que
    # crm_0003 se aplique sobre ella.
    old_exists = bind.execute(
        sa.text("select to_regclass('alembic_version_mod_crm_engine')")
    ).scalar()
    new_exists = bind.execute(
        sa.text("select to_regclass('alembic_version_pack_commerce')")
    ).scalar()
    if old_exists and not new_exists:
        bind.execute(
            sa.text("alter table alembic_version_mod_crm_engine rename to alembic_version_pack_commerce")
        )


def downgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(
        sa.text("select to_regclass('alembic_version_pack_commerce')")
    ).scalar()
    if exists:
        bind.execute(
            sa.text("alter table alembic_version_pack_commerce rename to alembic_version_mod_crm_engine")
        )
    bind.execute(sa.text("alter table plugin_configs drop constraint if exists plugin_configs_plugin_id_fkey"))
    bind.execute(
        sa.text("update plugin_configs set plugin_id = 'mod_crm_engine' where plugin_id = 'pack_commerce'")
    )
    bind.execute(
        sa.text("update plugins set id = 'mod_crm_engine' where id = 'pack_commerce'")
    )
    bind.execute(
        sa.text(
            "alter table plugin_configs add constraint plugin_configs_plugin_id_fkey "
            "foreign key (plugin_id) references plugins(id)"
        )
    )
    for spec_id, spec in bind.execute(
        sa.text("select id, spec from agent_specs")
    ).fetchall():
        spec = dict(spec)
        if "pack_commerce" in (spec.get("packs", []) or []):
            spec["packs"] = ["mod_crm_engine" if p == "pack_commerce" else p for p in spec.get("packs", [])]
            bind.execute(
                sa.text("update agent_specs set spec = :spec where id = :id"),
                {"spec": json.dumps(spec), "id": spec_id},
            )
    bind.execute(
        sa.text("update plugin_configs set plugin_id = 'mod_crm_engine' where plugin_id = 'pack_commerce'")
    )
    bind.execute(
        sa.text("update plugins set id = 'mod_crm_engine' where id = 'pack_commerce'")
    )