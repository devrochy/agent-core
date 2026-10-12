"""Alcance de la promesa de pago: por lo vencido o por el saldo total (HU-AOS-08, 2026-10-11).

Decisión del dueño: la promesa del procedimiento `pack_cobranza.promesa_de_pago` puede ser por lo
vencido o por el saldo total (adenda HU-AOS-08 de ADR-0113).

- `pack_cobranza_reglas_negociacion.alcance_promesa`: los alcances que acepta el negocio, junto a
  las demás reglas de negociación (`ambos` por defecto). Columna aditiva con default constante:
  no reescribe la tabla.
- `pack_cobranza_promesas.alcance`: por qué monto fue la promesa (`NULL` en las de antes, las del
  gestor y las de la tool de siempre), para el seguimiento de cumplida/incumplida.

Los CHECK se crean `NOT VALID` y se validan aparte (el `VALIDATE` toma un lock que no bloquea
lecturas ni escrituras). Las tablas ya tienen `tenant_id`, RLS forzada y su policy.

Revision ID: pack_cobranza_0017
Revises: pack_cobranza_0016
Create Date: 2026-10-11
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "pack_cobranza_0017"
down_revision = "pack_cobranza_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pack_cobranza_reglas_negociacion",
        sa.Column("alcance_promesa", sa.Text(), nullable=False, server_default="ambos"),
    )
    op.execute(
        "ALTER TABLE pack_cobranza_reglas_negociacion ADD CONSTRAINT ck_pack_cobranza_reglas_alcance_promesa "
        "CHECK (alcance_promesa IN ('ambos', 'vencido', 'saldo_total')) NOT VALID"
    )
    op.execute(
        "ALTER TABLE pack_cobranza_reglas_negociacion VALIDATE CONSTRAINT ck_pack_cobranza_reglas_alcance_promesa"
    )
    op.add_column("pack_cobranza_promesas", sa.Column("alcance", sa.Text(), nullable=True))
    op.execute(
        "ALTER TABLE pack_cobranza_promesas ADD CONSTRAINT ck_pack_cobranza_promesas_alcance "
        "CHECK (alcance IS NULL OR alcance IN ('vencido', 'saldo_total')) NOT VALID"
    )
    op.execute("ALTER TABLE pack_cobranza_promesas VALIDATE CONSTRAINT ck_pack_cobranza_promesas_alcance")


def downgrade() -> None:
    op.drop_constraint("ck_pack_cobranza_promesas_alcance", "pack_cobranza_promesas", type_="check")
    op.drop_column("pack_cobranza_promesas", "alcance")
    op.drop_constraint(
        "ck_pack_cobranza_reglas_alcance_promesa", "pack_cobranza_reglas_negociacion", type_="check"
    )
    op.drop_column("pack_cobranza_reglas_negociacion", "alcance_promesa")
