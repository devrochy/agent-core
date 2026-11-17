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
    # Con la unicidad vieja no puede haber duplicados. Solo tras una ida y vuelta de desarrollo
    # (la reversa pierde el horario) dos versiones pueden quedar iguales: entonces el índice queda sin
    # unicidad en vez de romper la migración.
    conn = op.get_bind()
    conn.execute(sa.text("SET LOCAL row_security = off"))
    repetidas = conn.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT 1 FROM agent_versions "
            "GROUP BY tenant_id, agent_id, content_hash, coalesce(flow_hash, '') HAVING count(*) > 1) d"
        )
    ).scalar()
    unico = "" if repetidas else "UNIQUE "
    op.execute(
        f"CREATE {unico}INDEX {_UNICO} ON agent_versions "
        "(tenant_id, agent_id, content_hash, coalesce(flow_hash, ''), coalesce(schedule_hash, ''))"
    )
    op.execute("ALTER TABLE agent_versions DROP CONSTRAINT IF EXISTS uq_agent_versions_hash")


def downgrade() -> None:
    # Vuelve al esquema anterior sin bloquear (la prueba de migraciones baja todo el árbol): se pierde
    # el horario propio (los agentes heredan el del negocio, la conducta anterior). La unicidad vieja
    # por contenido solo se restaura si no hay dos versiones del mismo contenido (distintas por
    # flujo u horario); si las hay, queda sin ella para no perder cuál es cuál.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP INDEX IF EXISTS {_UNICO}")
    op.drop_column("agent_versions", "schedule_hash")
    op.drop_column("agent_versions", "schedule")
    op.drop_column("agent_drafts", "schedule")
    conn = op.get_bind()
    conn.execute(sa.text("SET LOCAL row_security = off"))
    repetidas = conn.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT 1 FROM agent_versions GROUP BY tenant_id, agent_id, content_hash "
            "HAVING count(*) > 1) d"
        )
    ).scalar()
    if not repetidas:
        op.execute(
            "ALTER TABLE agent_versions ADD CONSTRAINT uq_agent_versions_hash UNIQUE (tenant_id, agent_id, content_hash)"
        )
