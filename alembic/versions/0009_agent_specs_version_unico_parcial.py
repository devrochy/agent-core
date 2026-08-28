"""uq_agent_specs_tenant_version → índice único PARCIAL (invariante #8).

El constraint UNIQUE completo bloquea reutilizar un número de versión tras un
soft-delete: las versiones borradas lógicamente siguen ocupando (tenant, version),
rompiendo la invariante "deleted_at + índice único parcial". Se cambia a un
índice único parcial WHERE deleted_at IS NULL (mismo patrón que uq_agent_specs_active).

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-28
"""
from __future__ import annotations

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table agent_specs drop constraint if exists uq_agent_specs_tenant_version")
    op.execute(
        "create unique index if not exists uq_agent_specs_tenant_version "
        "on agent_specs (tenant_id, version) where deleted_at is null"
    )


def downgrade() -> None:
    op.execute("drop index if exists uq_agent_specs_tenant_version")
    op.execute(
        "alter table agent_specs add constraint uq_agent_specs_tenant_version "
        "unique (tenant_id, version)"
    )