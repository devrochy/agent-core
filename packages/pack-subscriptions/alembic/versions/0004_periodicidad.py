"""Periodicidad y día ancla de la suscripción (HU-SUB-04).

Antes el próximo cargo era siempre `+30 días` (un plan anual se cobraba cada mes y
un mensual se corría de día). Ahora el período sale del calendario:
`periodicidad` (`mensual|trimestral|anual`) y `dia_ancla` (día del mes en que
empezó, para volver al 31 después de febrero).

Columnas nuevas con default constante / nullables: sin reescritura de tabla.
Backfill: `dia_ancla` desde la fecha de alta (o de creación).

Revision ID: pack_subscriptions_0004
Revises: pack_subscriptions_0003
Create Date: 2026-10-08
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "pack_subscriptions_0004"
down_revision = "pack_subscriptions_0003"
branch_labels = None
depends_on = None

TABLA = "pack_subscriptions_subscriptions"


def upgrade() -> None:
    op.add_column(
        TABLA,
        sa.Column("periodicidad", sa.Text(), nullable=False, server_default=sa.text("'mensual'")),
    )
    op.create_check_constraint(
        "ck_pack_subscriptions_periodicidad",
        TABLA,
        "periodicidad in ('mensual','trimestral','anual')",
    )
    op.add_column(TABLA, sa.Column("dia_ancla", sa.SmallInteger(), nullable=True))
    op.execute(
        f"update {TABLA} set dia_ancla = extract(day from coalesce(fecha_alta, created_at))::smallint "
        "where dia_ancla is null"
    )


def downgrade() -> None:
    op.drop_column(TABLA, "dia_ancla")
    op.drop_constraint("ck_pack_subscriptions_periodicidad", TABLA, type_="check")
    op.drop_column(TABLA, "periodicidad")
