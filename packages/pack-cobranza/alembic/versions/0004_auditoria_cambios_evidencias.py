"""Auditoría de cartera, cambios de cliente y evidencias (bloque A).

Revision ID: pack_cobranza_0004
Revises: pack_cobranza_0003
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "pack_cobranza_0004"
down_revision = "pack_cobranza_0003"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_TS = sa.DateTime(timezone=True)
_UUID = UUID(as_uuid=True)

_NEW_TABLES = ("pack_cobranza_auditoria", "pack_cobranza_cambios_cliente", "pack_cobranza_cliente_evidencias")


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )


def upgrade() -> None:
    op.create_table(
        "pack_cobranza_auditoria",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("entidad", sa.Text(), nullable=False),
        sa.Column("entidad_id", _UUID, nullable=False),
        sa.Column("operacion", sa.Text(), nullable=False),
        sa.Column("valores_antes", JSONB(), nullable=True),
        sa.Column("valores_despues", JSONB(), nullable=True),
        sa.Column("actor_rol", sa.Text(), nullable=True),
        sa.Column("actor_id", _UUID, nullable=True),
        sa.Column("motivo", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_pack_cobranza_auditoria_tenant_entidad", "pack_cobranza_auditoria", ["tenant_id", "entidad", "entidad_id"])
    _rls("pack_cobranza_auditoria")

    op.create_table(
        "pack_cobranza_cambios_cliente",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("cliente_id", _UUID, nullable=False),
        sa.Column("campos_propuestos", JSONB(), nullable=False, server_default="{}"),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("solicitado_por_rol", sa.Text(), nullable=True),
        sa.Column("solicitado_por_id", _UUID, nullable=True),
        sa.Column("revisado_por", _UUID, nullable=True),
        sa.Column("revisado_en", _TS, nullable=True),
        sa.Column("motivo_rechazo", sa.Text(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_pack_cobranza_cambios_cliente_tenant_cliente", "pack_cobranza_cambios_cliente", ["tenant_id", "cliente_id"])
    _rls("pack_cobranza_cambios_cliente")

    op.create_table(
        "pack_cobranza_cliente_evidencias",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("cliente_id", _UUID, nullable=False),
        sa.Column("tipo", sa.Text(), nullable=False),
        sa.Column("ruta_archivo", sa.Text(), nullable=False),
        sa.Column("nombre_original", sa.Text(), nullable=True),
        sa.Column("mimetype", sa.Text(), nullable=True),
        sa.Column("tamano", sa.Integer(), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_pack_cobranza_cliente_evidencias_tenant_cliente", "pack_cobranza_cliente_evidencias", ["tenant_id", "cliente_id"])
    _rls("pack_cobranza_cliente_evidencias")

    # FORCE RLS (ADR-0035): las tablas nuevas no las alcanza la migración 0003.
    for table in _NEW_TABLES:
        op.execute(f'alter table "{table}" force row level security')


def downgrade() -> None:
    for table in reversed(_NEW_TABLES):
        op.execute(f"drop policy if exists tenant_isolation on {table}")
        op.drop_table(table)
