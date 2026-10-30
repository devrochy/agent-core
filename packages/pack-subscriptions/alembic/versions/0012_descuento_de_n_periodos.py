"""Descuento de N períodos (SPEC-43, HU-SUB-35, ADR-0177 §Promociones de plan).

Agrega a `pack_subscriptions_subscriptions` el descuento vigente de la suscripción: `promocion_id`,
`descuento` (fotografía: promoción, canje, tipo, valor, `periodos_total`) y `descuento_periodos_restantes`.
Las tres son NULL o las tres tienen valor, y `restantes` va de 1 a `periodos_total`: al consumirse el último
período las columnas vuelven a NULL y el canje pasa a `agotado`.

Crea `uq_pack_subscriptions_charges_periodo`: un solo cargo de período no anulado por suscripción y etiqueta
de período. Es la red de seguridad del consumo único del descuento (dos corridas del cron, o el cron y
`charge_link`, nunca crean dos cargos del mismo período). Si ya hay duplicados la migración se detiene con el
detalle: no los anula por su cuenta.

Backfill: los canjes de un solo período que quedaron `confirmado` (HU-SUB-33) pasan a `agotado`, que es su
estado terminal.

Reversible. Las tablas no cambian de privilegios ni de RLS.

Revision ID: pack_subscriptions_0012
Revises: pack_subscriptions_0011
Create Date: 2026-10-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "pack_subscriptions_0012"
down_revision = "pack_subscriptions_0011"
branch_labels = None
depends_on = None

SUBS = "pack_subscriptions_subscriptions"
CHARGES = "pack_subscriptions_charges"
CANJES = "pack_subscriptions_canjes"
PROMOS = "pack_subscriptions_promociones"


def upgrade() -> None:
    # 1. Sin duplicados de período: el único parcial no se puede crear si ya los hay.
    duplicados = op.get_bind().execute(
        sa.text(
            f"""
            select tenant_id, subscription_id, periodo, count(*) as n
              from {CHARGES}
             where tipo = 'periodo' and estado <> 'anulado' and deleted_at is null
             group by tenant_id, subscription_id, periodo
            having count(*) > 1
             limit 10
            """
        )
    ).fetchall()
    if duplicados:
        detalle = "; ".join(f"{d.tenant_id}/{d.subscription_id}/{d.periodo} x{d.n}" for d in duplicados)
        raise RuntimeError(f"Hay cargos de período duplicados: resuélvelos antes de migrar ({detalle}).")

    # 2. Descuento vigente en la suscripción.
    op.add_column(SUBS, sa.Column("promocion_id", UUID(as_uuid=True), nullable=True))
    op.add_column(SUBS, sa.Column("descuento", JSONB, nullable=True))
    op.add_column(SUBS, sa.Column("descuento_periodos_restantes", sa.SmallInteger, nullable=True))
    op.create_foreign_key(
        "fk_pack_subscriptions_subs_promocion",
        SUBS,
        PROMOS,
        ["tenant_id", "promocion_id"],
        ["tenant_id", "id"],
    )
    op.create_check_constraint(
        "ck_pack_subscriptions_subs_descuento_par",
        SUBS,
        "(promocion_id is null) = (descuento is null) and (promocion_id is null) = (descuento_periodos_restantes is null)",
    )
    op.create_check_constraint(
        "ck_pack_subscriptions_subs_descuento_restantes",
        SUBS,
        "descuento is null or (jsonb_typeof(descuento) = 'object'"
        " and descuento_periodos_restantes >= 1"
        " and descuento_periodos_restantes <= (descuento->>'periodos_total')::int)",
    )
    op.create_index(
        "ix_pack_subscriptions_subs_tenant_promocion",
        SUBS,
        ["tenant_id", "promocion_id"],
        postgresql_where=sa.text("promocion_id is not null"),
    )

    # 3. Un solo cargo de período no anulado por suscripción y período.
    op.create_index(
        "uq_pack_subscriptions_charges_periodo",
        CHARGES,
        ["tenant_id", "subscription_id", "periodo"],
        unique=True,
        postgresql_where=sa.text("tipo = 'periodo' and estado <> 'anulado' and deleted_at is null"),
    )

    # 4. Backfill: el canje de un solo período que ya se cobró es terminal (`agotado`).
    # El dueño de la tabla con RLS forzada no ve filas sin `app.tenant_id`: se apaga y se reenciende.
    op.execute(f"alter table {CANJES} no force row level security")
    op.execute(f"alter table {CANJES} disable row level security")
    op.execute(f"update {CANJES} set estado = 'agotado' where estado = 'confirmado' and periodos_total = 1")
    op.execute(f"alter table {CANJES} enable row level security")
    op.execute(f"alter table {CANJES} force row level security")


def downgrade() -> None:
    op.drop_index("uq_pack_subscriptions_charges_periodo", table_name=CHARGES)
    op.drop_index("ix_pack_subscriptions_subs_tenant_promocion", table_name=SUBS)
    op.drop_constraint("ck_pack_subscriptions_subs_descuento_restantes", SUBS, type_="check")
    op.drop_constraint("ck_pack_subscriptions_subs_descuento_par", SUBS, type_="check")
    op.drop_constraint("fk_pack_subscriptions_subs_promocion", SUBS, type_="foreignkey")
    for columna in ("descuento_periodos_restantes", "descuento", "promocion_id"):
        op.drop_column(SUBS, columna)
    # El backfill no se deshace: `agotado` y `confirmado` son terminales para el esquema anterior.
