"""Referencia los playbooks del pack CRM en el AgentSpec del tenant CULTO.

Los playbooks de CULTO (ventas, seguimiento) viven en el pack
(plugins/mod_crm_engine/playbooks/) y entran al system prompt por la allowlist
`AgentSpec.playbooks`. Esta migración los añade al índice del spec activo del
tenant #1 (CULTO) si faltan.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-26
"""
from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

TENANT1 = "00000000-0000-0000-0000-000000000001"

PACK_PLAYBOOKS = [
    {"name": "ventas", "description": "Reglas del proceso de venta"},
    {"name": "seguimiento", "description": "Reglas de seguimiento y reenganche"},
]


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "select id, spec from agent_specs "
            "where tenant_id = :t and is_active and deleted_at is null"
        ),
        {"t": TENANT1},
    ).fetchall()
    for spec_id, spec in rows:
        spec = dict(spec)
        existing = {p.get("name") for p in spec.get("playbooks", [])}
        missing = [p for p in PACK_PLAYBOOKS if p["name"] not in existing]
        if missing:
            spec["playbooks"] = [*spec.get("playbooks", []), *missing]
            bind.execute(
                sa.text("update agent_specs set spec = :spec where id = :id"),
                {"spec": json.dumps(spec), "id": spec_id},
            )


def downgrade() -> None:
    bind = op.get_bind()
    names = [p["name"] for p in PACK_PLAYBOOKS]
    rows = bind.execute(
        sa.text(
            "select id, spec from agent_specs "
            "where tenant_id = :t and is_active and deleted_at is null"
        ),
        {"t": TENANT1},
    ).fetchall()
    for spec_id, spec in rows:
        spec = dict(spec)
        spec["playbooks"] = [p for p in spec.get("playbooks", []) if p.get("name") not in names]
        bind.execute(
            sa.text("update agent_specs set spec = :spec where id = :id"),
            {"spec": json.dumps(spec), "id": spec_id},
        )