"""Suscripción recurrente por cliente (ADR-0082, Opción 2).

Nueva tabla `pack_cobranza_suscripciones` (el cliente paga un valor por período) y
`pack_cobranza_cuotas` gana `suscripcion_id` (el cargo del período cuelga de la
suscripción; `prestamo_id` pasa a nullable).

Revision ID: pack_cobranza_0013
Revises: pack_cobranza_0012
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "pack_cobranza_0013"
down_revision = "pack_cobranza_0012"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)
_UUID = UUID(as_uuid=True)
_NUM = sa.Numeric(14, 2)


def upgrade() -> None:
    op.create_table(
        "pack_cobranza_suscripciones",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("cliente_id", _UUID, nullable=False),
        sa.Column("cartera_id", _UUID, nullable=True),
        sa.Column("concepto", sa.Text(), nullable=False),
        sa.Column("valor_periodo", _NUM, nullable=False, server_default="0"),
        sa.Column("moneda", sa.Text(), nullable=False, server_default="COP"),
        sa.Column("periodicidad", sa.Text(), nullable=False, server_default="mensual"),
        sa.Column("dia_facturacion", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("estado", sa.Text(), nullable=False, server_default="activa"),
        sa.Column("proximo_cargo", sa.Date(), nullable=True),
        sa.Column("suscrito_en", _TS, nullable=True),
        sa.Column("cancelado_en", _TS, nullable=True),
        sa.Column("notas", sa.Text(), nullable=False, server_default=""),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index(
        "ix_pack_cobranza_suscripciones_tenant_cliente",
        "pack_cobranza_suscripciones",
        ["tenant_id", "cliente_id"],
    )
    op.create_index(
        "ix_pack_cobranza_suscripciones_tenant_proximo",
        "pack_cobranza_suscripciones",
        ["tenant_id", "proximo_cargo"],
    )
    op.execute("alter table pack_cobranza_suscripciones enable row level security")
    op.execute(
        "create policy tenant_isolation on pack_cobranza_suscripciones "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    op.execute("alter table pack_cobranza_suscripciones force row level security")
    op.execute(
        "grant select, insert, update, delete on pack_cobranza_suscripciones to app_runtime"
    )

    # El cargo del período cuelga de la suscripción (no de un préstamo).
    op.alter_column(
        "pack_cobranza_cuotas", "prestamo_id", existing_type=_UUID, nullable=True
    )
    op.add_column("pack_cobranza_cuotas", sa.Column("suscripcion_id", _UUID, nullable=True))
    op.create_index(
        "ix_pack_cobranza_cuotas_tenant_suscripcion",
        "pack_cobranza_cuotas",
        ["tenant_id", "suscripcion_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_pack_cobranza_cuotas_tenant_suscripcion", table_name="pack_cobranza_cuotas")
    op.drop_column("pack_cobranza_cuotas", "suscripcion_id")
    op.alter_column(
        "pack_cobranza_cuotas", "prestamo_id", existing_type=_UUID, nullable=False
    )
    op.execute("drop policy if exists tenant_isolation on pack_cobranza_suscripciones")
    op.execute("alter table pack_cobranza_suscripciones no force row level security")
    op.drop_index(
        "ix_pack_cobranza_suscripciones_tenant_proximo", table_name="pack_cobranza_suscripciones"
    )
    op.drop_index(
        "ix_pack_cobranza_suscripciones_tenant_cliente", table_name="pack_cobranza_suscripciones"
    )
    op.drop_table("pack_cobranza_suscripciones")
