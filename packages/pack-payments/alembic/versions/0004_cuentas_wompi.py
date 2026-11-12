"""Cuentas Wompi del negocio que cada agente elige (ADR-0190 §6, HU-AOS-62).

`pack_payments_accounts`: una fila por cuenta Wompi del negocio. La llave privada y el secreto de eventos van en
`secretos_cifrados` (AES-GCM con AAD `(tenant_id, cuenta_id)`, nunca en claro); la llave pública, el ambiente, la
vigencia del link y el mínimo de pago en línea van en claro porque no son secretos y los usan crons y panel. Tabla de
NEGOCIO (invariante 1): `tenant_id`, RLS habilitada y FORZADA, índices que empiezan por `tenant_id`, `UNIQUE
(tenant_id, id)` para las FKs compuestas y borrado lógico (invariante 8) con nombre único entre las vivas.

`pack_payments_transactions` suma `cuenta_id` (FK compuesta a la cuenta) y `agent_id` (el agente que pidió el
link; sin FK porque `agents` es del core y el pack no depende de su esquema). Quedan nulos mientras convivan los
links pedidos con la config del negocio (`plugin_configs`); la resiembra (HU-AOS-65) los vuelve obligatorios.

Reversible: el downgrade se niega si hay cuentas vivas (no se borran datos de negocio).

Revision ID: pack_payments_0004
Revises: pack_payments_0003
Create Date: 2026-11-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "pack_payments_0004"
down_revision = "pack_payments_0003"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_UUID = UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_TABLA = "pack_payments_accounts"


def upgrade() -> None:
    op.create_table(
        _TABLA,
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("nombre", sa.Text(), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("secretos_cifrados", sa.Text(), nullable=False),
        sa.Column("sandbox", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("link_expira_horas", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("monto_minimo", sa.Numeric(12, 2), nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_pack_payments_accounts_tenant_id"),
        sa.CheckConstraint("char_length(nombre) BETWEEN 1 AND 80", name="ck_pack_payments_accounts_nombre"),
        sa.CheckConstraint("link_expira_horas BETWEEN 1 AND 720", name="ck_pack_payments_accounts_vigencia"),
        sa.CheckConstraint("monto_minimo IS NULL OR monto_minimo >= 0", name="ck_pack_payments_accounts_minimo"),
        sa.CheckConstraint("secretos_cifrados LIKE 'gcm:v1:%'", name="ck_pack_payments_accounts_cifrado"),
    )
    op.create_index(
        "uq_pack_payments_accounts_nombre",
        _TABLA,
        ["tenant_id", sa.text("lower(nombre)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.execute(f"ALTER TABLE {_TABLA} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLA} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {_TABLA} USING (tenant_id = {RLS}) WITH CHECK (tenant_id = {RLS})"
    )

    op.add_column("pack_payments_transactions", sa.Column("cuenta_id", _UUID, nullable=True))
    op.add_column("pack_payments_transactions", sa.Column("agent_id", _UUID, nullable=True))
    op.create_foreign_key(
        "fk_pack_payments_transactions_cuenta",
        "pack_payments_transactions",
        _TABLA,
        ["tenant_id", "cuenta_id"],
        ["tenant_id", "id"],
    )
    op.create_index(
        "ix_pack_payments_transactions_cuenta",
        "pack_payments_transactions",
        ["tenant_id", "cuenta_id"],
        postgresql_where=sa.text("cuenta_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM {_TABLA} WHERE deleted_at IS NULL) THEN
            RAISE EXCEPTION 'hay cuentas Wompi vivas: no se bajan sin decidir qué hacer con ellas';
          END IF;
        END $$;
        """
    )
    op.drop_index("ix_pack_payments_transactions_cuenta", table_name="pack_payments_transactions")
    op.drop_constraint("fk_pack_payments_transactions_cuenta", "pack_payments_transactions", type_="foreignkey")
    op.drop_column("pack_payments_transactions", "agent_id")
    op.drop_column("pack_payments_transactions", "cuenta_id")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLA}")
    op.drop_table(_TABLA)
