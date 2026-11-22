"""`agents.nivel_ia`: el nivel de IA que el negocio elige por agente (ADR-0210 §1, HU-BILL-24).

`estandar` | `avanzado`, con `estandar` por defecto. El core lo traduce al modelo al publicar la versión
(`core/llm/niveles.py`, `versiones.publicar`); el contrato `AgentSpecV3` no cambia (invariante 4).

`agents` ya tiene `tenant_id`, RLS forzada y policy: la columna nueva no cambia el aislamiento. Aditiva: el
`server_default` llena las filas existentes sin reescribir datos de negocio y el CHECK solo admite los dos niveles.

Reversible: el downgrade quita el CHECK y la columna.

Revision ID: 0153
Revises: 0152
Create Date: 2026-11-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0153"
down_revision = "0152"
branch_labels = None
depends_on = None

_CHECK = "ck_agents_nivel_ia"


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("nivel_ia", sa.Text(), nullable=False, server_default=sa.text("'estandar'")),
    )
    op.create_check_constraint(_CHECK, "agents", "nivel_ia in ('estandar', 'avanzado')")


def downgrade() -> None:
    op.drop_constraint(_CHECK, "agents", type_="check")
    op.drop_column("agents", "nivel_ia")
