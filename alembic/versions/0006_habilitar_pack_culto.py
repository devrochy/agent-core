"""Habilita el pack del CRM en el AgentSpec del tenant CULTO (ADR-0019).

El seed por defecto deja `spec.packs = []`; sin packs declarados, el enablement
por tenant filtraría las skills del CRM y CULTO quedaría sin tools. Esta migración
declara el pack en el spec activo del tenant #1.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-26
"""
from __future__ import annotations

import json

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TENANT1 = "00000000-0000-0000-0000-000000000001"
PACK_ID = "mod_crm_engine"


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
        packs = list(spec.get("packs", []) or [])
        if PACK_ID not in packs:
            packs.append(PACK_ID)
            spec["packs"] = packs
            bind.execute(
                sa.text("update agent_specs set spec = :spec where id = :id"),
                {"spec": json.dumps(spec), "id": spec_id},
            )


def downgrade() -> None:
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
        spec["packs"] = [p for p in spec.get("packs", []) if p != PACK_ID]
        bind.execute(
            sa.text("update agent_specs set spec = :spec where id = :id"),
            {"spec": json.dumps(spec), "id": spec_id},
        )