"""Operación de cartera (bloque B): notas, aperturas, posiciones, reportes,
estadísticas y log de optimización.

Revision ID: pack_cobranza_0005
Revises: pack_cobranza_0004
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "pack_cobranza_0005"
down_revision = "pack_cobranza_0004"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)
_UUID = UUID(as_uuid=True)
_NUM = sa.Numeric(14, 2)

_NEW_TABLES = (
    "pack_cobranza_cartera_notas",
    "pack_cobranza_carteras_aperturas",
    "pack_cobranza_posicion_gestor",
    "pack_cobranza_reportes_diarios",
    "pack_cobranza_cartera_estadisticas",
    "pack_cobranza_cartera_optimizada",
)


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )


def _head(table: str, index: str, cols: list[str], *, unique: bool = False) -> None:
    op.create_index(index, table, ["tenant_id", *cols], unique=unique)


def upgrade() -> None:
    op.create_table(
        "pack_cobranza_cartera_notas",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("cartera_id", _UUID, nullable=False), sa.Column("nota", sa.Text(), nullable=False),
        sa.Column("creado_por_rol", sa.Text(), nullable=True), sa.Column("creado_por_id", _UUID, nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    _head("pack_cobranza_cartera_notas", "ix_pack_cobranza_cartera_notas_tenant_cartera", ["cartera_id"])
    _rls("pack_cobranza_cartera_notas")

    op.create_table(
        "pack_cobranza_carteras_aperturas",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("cartera_id", _UUID, nullable=False), sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("hora_inicio", sa.Text(), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True), sa.Column("lng", sa.Float(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    _head("pack_cobranza_carteras_aperturas", "ix_pack_cobranza_carteras_aperturas_tenant_cartera", ["cartera_id", "fecha"])
    _rls("pack_cobranza_carteras_aperturas")

    op.create_table(
        "pack_cobranza_posicion_gestor",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("gestor_id", _UUID, nullable=True), sa.Column("cartera_id", _UUID, nullable=False),
        sa.Column("lat", sa.Float(), nullable=False), sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("registrada_en", _TS, nullable=False, server_default=sa.text("now()")),
    )
    _head("pack_cobranza_posicion_gestor", "ix_pack_cobranza_posicion_gestor_tenant_cartera", ["cartera_id"])
    _rls("pack_cobranza_posicion_gestor")

    op.create_table(
        "pack_cobranza_reportes_diarios",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("cartera_id", _UUID, nullable=False), sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("cobrado", _NUM, nullable=False, server_default="0"), sa.Column("prestado", _NUM, nullable=False, server_default="0"),
        sa.Column("clientes_visitados", JSONB(), nullable=True), sa.Column("clientes_sin_pago", JSONB(), nullable=True),
        sa.Column("trayectorias", JSONB(), nullable=True),
        sa.Column("hora_inicio", sa.Text(), nullable=True), sa.Column("hora_fin", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    _head("pack_cobranza_reportes_diarios", "ix_pack_cobranza_reportes_diarios_tenant_cartera", ["cartera_id", "fecha"])
    _rls("pack_cobranza_reportes_diarios")

    op.create_table(
        "pack_cobranza_cartera_estadisticas",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("cartera_id", _UUID, nullable=False), sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("datos", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    _head("pack_cobranza_cartera_estadisticas", "ix_pack_cobranza_cartera_estadisticas_tenant_cartera", ["cartera_id", "fecha"])
    _rls("pack_cobranza_cartera_estadisticas")

    op.create_table(
        "pack_cobranza_cartera_optimizada",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("cartera_id", _UUID, nullable=False), sa.Column("fecha", sa.Date(), nullable=False),
        sa.Column("orden_clientes", JSONB(), nullable=True),
        sa.Column("distancia_estimada_km", sa.Numeric(10, 2), nullable=True), sa.Column("tiempo_estimado_min", sa.Integer(), nullable=True),
        sa.Column("recalculado", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("motivo_recalculo", sa.Text(), nullable=True),
        sa.Column("tipo", sa.Text(), nullable=False, server_default="planificada"),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    _head("pack_cobranza_cartera_optimizada", "ix_pack_cobranza_cartera_optimizada_tenant_cartera", ["cartera_id", "fecha"])
    _rls("pack_cobranza_cartera_optimizada")

    for table in _NEW_TABLES:
        op.execute(f'alter table "{table}" force row level security')


def downgrade() -> None:
    for table in reversed(_NEW_TABLES):
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.drop_table(table)
