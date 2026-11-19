"""Esquema inicial de pack-subscriptions (ADR-0083, Opción B).

Planes → Suscripción de cliente → Cargos por período. Greenfield: `tenant_id
NOT NULL` desde el arranque, RLS + policy de aislamiento e índices que empiezan
por `tenant_id`. `customer_id`/`variante_id` son referencias lógicas (UUID sin
constraint) al `Customer` del core y al catálogo de commerce respectivamente —
ningún pack importa a otro (invariante nº5).

Revision ID: pack_subscriptions_0001
Revises:
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "pack_subscriptions_0001"
down_revision = None
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)
_NUM = sa.Numeric(14, 2)
_UUID = UUID(as_uuid=True)


def _id() -> sa.Column:
    return sa.Column("id", _UUID, primary_key=True)


def _tenant() -> sa.Column:
    return sa.Column("tenant_id", _UUID, nullable=False)


def _audit() -> list[sa.Column]:
    return [
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("now()")),
    ]


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )


def upgrade() -> None:
    op.create_table(
        "pack_subscriptions_plans",
        _id(), _tenant(),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=False, server_default=""),
        sa.Column("precio", _NUM, nullable=False, server_default=sa.text("0")),
        sa.Column("moneda", sa.Text(), nullable=False, server_default=sa.text("'COP'")),
        sa.Column("periodicidad", sa.Text(), nullable=False, server_default=sa.text("'mensual'")),
        sa.Column("trial_dias", sa.Integer(), nullable=False, server_default=sa.text("30")),
        sa.Column("limites", JSONB, nullable=True),
        sa.Column("variante_id", _UUID, nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_audit(),
    )
    op.create_index(
        "ix_pack_subscriptions_plans_tenant", "pack_subscriptions_plans", ["tenant_id", "activo"]
    )
    _rls("pack_subscriptions_plans")

    op.create_table(
        "pack_subscriptions_subscriptions",
        _id(), _tenant(),
        sa.Column("customer_id", _UUID, nullable=False),
        sa.Column("plan_id", _UUID, nullable=True),
        sa.Column("variante_id", _UUID, nullable=True),
        sa.Column("concepto", sa.Text(), nullable=False, server_default=""),
        sa.Column("estado", sa.Text(), nullable=False, server_default=sa.text("'trialing'")),
        sa.Column("valor_periodo", _NUM, nullable=False, server_default=sa.text("0")),
        sa.Column("moneda", sa.Text(), nullable=False, server_default=sa.text("'COP'")),
        sa.Column("periodo_actual_inicio", _TS, nullable=True),
        sa.Column("periodo_actual_fin", _TS, nullable=True),
        sa.Column("proximo_cargo", _TS, nullable=True),
        sa.Column("trial_termina", _TS, nullable=True),
        sa.Column("fecha_alta", _TS, nullable=True),
        sa.Column("fecha_cancelacion", _TS, nullable=True),
        *_audit(),
    )
    op.create_index(
        "ix_pack_subscriptions_subs_tenant_customer",
        "pack_subscriptions_subscriptions", ["tenant_id", "customer_id"],
    )
    op.create_index(
        "ix_pack_subscriptions_subs_tenant_proximo",
        "pack_subscriptions_subscriptions", ["tenant_id", "proximo_cargo"],
    )
    _rls("pack_subscriptions_subscriptions")

    op.create_table(
        "pack_subscriptions_charges",
        _id(), _tenant(),
        sa.Column("subscription_id", _UUID, nullable=False),
        sa.Column("periodo", sa.Text(), nullable=False),
        sa.Column("monto", _NUM, nullable=False, server_default=sa.text("0")),
        sa.Column("estado", sa.Text(), nullable=False, server_default=sa.text("'pendiente'")),
        sa.Column("referencia", sa.Text(), nullable=True),
        sa.Column("fecha_vencimiento", _TS, nullable=True),
        *_audit(),
    )
    op.create_index(
        "ix_pack_subscriptions_charges_tenant_sub",
        "pack_subscriptions_charges", ["tenant_id", "subscription_id"],
    )
    _rls("pack_subscriptions_charges")


def downgrade() -> None:
    for table in ("pack_subscriptions_charges", "pack_subscriptions_subscriptions", "pack_subscriptions_plans"):
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.drop_table(table)