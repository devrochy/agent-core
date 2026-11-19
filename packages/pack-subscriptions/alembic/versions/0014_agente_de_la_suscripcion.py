"""El agente que creó la suscripción (ADR-0190 §4, HU-AOS-62).

Los cargos del período, recordatorios y reenvíos de link corren sin conversación (cron y panel). Para cobrar con la
cuenta Wompi del agente que vendió la suscripción, la suscripción guarda ese `agent_id`. Sin FK: `agents` es del core
y el pack no depende de su esquema (mismo criterio que `pack_payments_transactions.agent_id`). Nulo en las creadas
desde el panel o antes de esta revisión: cobran con la config del negocio.

Revision ID: pack_subscriptions_0014
Revises: pack_subscriptions_0013
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "pack_subscriptions_0014"
down_revision = "pack_subscriptions_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pack_subscriptions_subscriptions", sa.Column("agent_id", UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("pack_subscriptions_subscriptions", "agent_id")
