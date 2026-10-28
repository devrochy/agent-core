"""Escritura en caché del proveedor en `usage_ledger` (HU-AOS-35, adenda de ADR-0117).

Columna nueva `usage_ledger.cache_write_tokens` (integer, nullable): los tokens que el turno ESCRIBIÓ en la caché
del proveedor (Anthropic). Desde esta migración `cached_tokens` guarda solo lo LEÍDO y `input_tokens` el total de
entrada con lo cacheado incluido; antes `cached_tokens` mezclaba lectura y escritura. Las filas anteriores quedan
con NULL: el porcentaje cacheado por agente solo cuenta filas con la columna llena (mismo criterio que `credits`
antes de la 0096).

`usage_ledger` ya es tabla de negocio (tenant_id + RLS forzada desde su creación): no cambian policies ni índices.

Aditiva y reversible: `ADD COLUMN` nullable sin default no reescribe la tabla ni toma un bloqueo largo. El
downgrade elimina la columna (se pierde el dato de escritura en caché de las filas nuevas).

Revision ID: 0114
Revises: 0113
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_ledger", sa.Column("cache_write_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_ledger", "cache_write_tokens")
