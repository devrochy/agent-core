"""Flujo de atención por agente (ADR-0195, SPEC-46, HU-AOS-74).

El flujo deja de ser un artefacto del negocio y vive al lado del AgentSpec v3, en el
borrador y en la versión inmutable del agente. Las versiones anteriores quedan con
`flow` NULL y siguen leyendo `flow_tenant` de la release.

Revision ID: 0133
Revises: 0132
Create Date: 2026-11-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "agent_drafts",
        sa.Column("flow", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.add_column(
        "agent_versions",
        sa.Column("flow", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.add_column("agent_versions", sa.Column("flow_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_versions", "flow_hash")
    op.drop_column("agent_versions", "flow")
    op.drop_column("agent_drafts", "flow")
