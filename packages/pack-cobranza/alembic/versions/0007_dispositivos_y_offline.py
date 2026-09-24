"""Dispositivos, intentos de acceso y sincronización offline (bloque D, ADR-0057).

Revision ID: pack_cobranza_0007
Revises: pack_cobranza_0006
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "pack_cobranza_0007"
down_revision = "pack_cobranza_0006"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)
_UUID = UUID(as_uuid=True)

_NEW_TABLES = ("pack_cobranza_devices", "pack_cobranza_intentos_acceso", "pack_cobranza_sync_eventos")


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )


def upgrade() -> None:
    op.add_column("pack_cobranza_gestores", sa.Column("password_hash", sa.Text(), nullable=True))

    op.create_table(
        "pack_cobranza_devices",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("codigo", sa.Text(), nullable=False),
        sa.Column("api_key_hash", sa.Text(), nullable=False),
        sa.Column("gestor_id", _UUID, nullable=True), sa.Column("cartera_id", _UUID, nullable=True),
        sa.Column("imei", sa.Text(), nullable=True), sa.Column("whatsapp_number", sa.Text(), nullable=True),
        sa.Column("public_key", sa.Text(), nullable=True),
        sa.Column("estado", sa.Text(), nullable=False, server_default="activo"),
        sa.Column("fecha_vinculacion", _TS, nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("uq_pack_cobranza_device_codigo", "pack_cobranza_devices", ["tenant_id", "codigo"], unique=True)
    op.create_index("ix_pack_cobranza_devices_tenant_gestor", "pack_cobranza_devices", ["tenant_id", "gestor_id"])
    _rls("pack_cobranza_devices")

    op.create_table(
        "pack_cobranza_intentos_acceso",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("gestor_id", _UUID, nullable=True), sa.Column("device_id", _UUID, nullable=True),
        sa.Column("imei", sa.Text(), nullable=True), sa.Column("whatsapp_number", sa.Text(), nullable=True),
        sa.Column("resultado", sa.Text(), nullable=False), sa.Column("motivo", sa.Text(), nullable=True),
        sa.Column("ip", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_pack_cobranza_intentos_acceso_tenant_gestor", "pack_cobranza_intentos_acceso", ["tenant_id", "gestor_id"])
    _rls("pack_cobranza_intentos_acceso")

    op.create_table(
        "pack_cobranza_sync_eventos",
        sa.Column("id", _UUID, primary_key=True), sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("device_id", _UUID, nullable=True),
        sa.Column("evento_id_cliente", sa.Text(), nullable=False),
        sa.Column("tipo", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("estado", sa.Text(), nullable=False, server_default="aplicado"),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("uq_pack_cobranza_sync_evento", "pack_cobranza_sync_eventos", ["tenant_id", "evento_id_cliente"], unique=True)
    op.create_index("ix_pack_cobranza_sync_eventos_tenant_device", "pack_cobranza_sync_eventos", ["tenant_id", "device_id"])
    _rls("pack_cobranza_sync_eventos")

    for table in _NEW_TABLES:
        op.execute(f'alter table "{table}" force row level security')


def downgrade() -> None:
    for table in reversed(_NEW_TABLES):
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.drop_table(table)
    op.drop_column("pack_cobranza_gestores", "password_hash")
