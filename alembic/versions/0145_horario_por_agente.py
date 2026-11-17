"""Horario por agente (SPEC-47, HU-AOS-82, D-SPEC-47-5).

El horario propio de un agente vive al lado del AgentSpec v3, como el flujo (ADR-0195): en el
borrador (`agent_drafts.schedule`) y congelado en la versión (`agent_versions.schedule` y su hash).
`NULL` = hereda el horario del negocio (`AppSettings.escalation_config.business_hours`): sin
backfill, todos los agentes existentes siguen con el del negocio.

La unicidad de las versiones pasa de `(tenant_id, agent_id, content_hash)` a un índice único que
también mira el flujo y el horario: publicar un cambio solo de horario (o solo de flujo) es una
versión nueva y no choca con la anterior. Columnas nullable sin default: solo metadato.

Revision ID: 0145
Revises: 0143
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0145"
down_revision = "0143"
branch_labels = None
depends_on = None

_UNICO = "uq_agent_versions_contenido_flujo_horario"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("agent_drafts", sa.Column("schedule", postgresql.JSONB(none_as_null=True), nullable=True))
    op.add_column("agent_versions", sa.Column("schedule", postgresql.JSONB(none_as_null=True), nullable=True))
    op.add_column("agent_versions", sa.Column("schedule_hash", sa.Text(), nullable=True))
    op.execute(
        f"CREATE UNIQUE INDEX {_UNICO} ON agent_versions "
        "(tenant_id, agent_id, content_hash, coalesce(flow_hash, ''), coalesce(schedule_hash, ''))"
    )
    op.execute("ALTER TABLE agent_versions DROP CONSTRAINT uq_agent_versions_hash")


def downgrade() -> None:
    # Reversa solo si ninguna versión usa el horario ni repite contenido: volver a la unicidad vieja
    # con dos versiones del mismo contenido fallaría (y perdería cuál es cuál).
    conn = op.get_bind()
    con_horario = conn.execute(sa.text("SELECT count(*) FROM agent_versions WHERE schedule IS NOT NULL")).scalar()
    if con_horario:
        raise RuntimeError("0145: hay versiones con horario propio; la reversa lo perdería.")
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE agent_versions ADD CONSTRAINT uq_agent_versions_hash UNIQUE (tenant_id, agent_id, content_hash)"
    )
    op.execute(f"DROP INDEX IF EXISTS {_UNICO}")
    op.drop_column("agent_versions", "schedule_hash")
    op.drop_column("agent_versions", "schedule")
    op.drop_column("agent_drafts", "schedule")
