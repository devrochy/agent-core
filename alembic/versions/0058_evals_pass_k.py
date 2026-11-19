"""Evals de consistencia pass^k (HU-ARCH-28, ADR-0119).

Columnas aditivas sobre `eval_runs` y `eval_results`, que ya tienen `tenant_id` + RLS
forzada (0036):

- `eval_runs.k`, `pass_1`, `pass_k`: intentos por escenario, tasa de éxito media y
  fracción de escenarios que pasaron los k intentos; `simulator` (JSONB): modelo y
  semillas del cliente simulado.
- `eval_results.k`, `pass_1`, `pass_k`, `inestable`: lo mismo por escenario, y si pasó
  en algunos intentos y falló en otros; `state_assertions` (JSONB): aserciones sobre la
  base con esperado y real por intento; `simulator` (JSONB): traza del cliente simulado;
  `verifier` (JSONB): reservado para el verificador de afirmaciones (HU-ARCH-29).

Defaults constantes o nullable: sin reescritura de tabla ni bloqueo largo.

Revision ID: 0058
Revises: 0057
Create Date: 2026-10-08
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for tabla in ("eval_runs", "eval_results"):
        op.add_column(tabla, sa.Column("k", sa.Integer(), nullable=False, server_default=sa.text("1")))
        op.add_column(tabla, sa.Column("pass_1", sa.Numeric(5, 4), nullable=True))
        op.add_column(tabla, sa.Column("pass_k", sa.Numeric(5, 4), nullable=True))
        op.add_column(tabla, sa.Column("simulator", JSONB(), nullable=True))
    op.add_column(
        "eval_results", sa.Column("inestable", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )
    op.add_column("eval_results", sa.Column("state_assertions", JSONB(), nullable=True))
    op.add_column("eval_results", sa.Column("verifier", JSONB(), nullable=True))


def downgrade() -> None:
    for columna in ("verifier", "state_assertions", "inestable"):
        op.drop_column("eval_results", columna)
    for tabla in ("eval_results", "eval_runs"):
        for columna in ("simulator", "pass_k", "pass_1", "k"):
            op.drop_column(tabla, columna)
