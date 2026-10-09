"""Aprovisionamiento automático de negocios (HU-SH-03, ADR-0120).

- `provisioning_requests`: una fila por alta automática, en el negocio VENDEDOR (`tenant_id`),
  con RLS habilitada y forzada. Es la llave de idempotencia del consumidor de
  `subscription.activated`: única por `(tenant_id, referencia)` y por
  `(tenant_id, subscription_id)` mientras no esté borrada. `negocio_tenant_id` apunta al
  negocio creado (`ON DELETE SET NULL`: purgar ese negocio no rompe el historial del
  vendedor, que conserva el slug).
- `tenant_invitations.token_nonce`: nonce del enlace de un solo uso de la invitación
  (ADR-0120 § 3.8). NULL = invitación sin enlace, la de siempre (ADR-0081).

Tabla nueva y vacía, columna nullable sin default: sin reescritura ni bloqueo largo.

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-09
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

_ESTADOS = "('en_curso', 'completado', 'revision', 'omitido')"


def upgrade() -> None:
    op.create_table(
        "provisioning_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("referencia", sa.Text(), nullable=False),
        sa.Column("subscription_id", sa.Uuid(), nullable=True),
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("estado", sa.Text(), nullable=False, server_default="en_curso"),
        sa.Column("plan_id", sa.Text(), nullable=True),
        sa.Column("vertical", sa.Text(), nullable=True),
        sa.Column("negocio_tenant_id", sa.Uuid(), nullable=True),
        sa.Column("negocio_slug", sa.Text(), nullable=True),
        sa.Column("invitation_id", sa.Uuid(), nullable=True),
        sa.Column("motivo", sa.Text(), nullable=True),
        sa.Column("detalle", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["negocio_tenant_id"],
            ["tenants.id"],
            name="fk_provisioning_requests_negocio",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(f"estado in {_ESTADOS}", name="ck_provisioning_requests_estado"),
        sa.CheckConstraint(
            "estado <> 'completado' or negocio_slug is not null",
            name="ck_provisioning_requests_completado_con_negocio",
        ),
        sa.CheckConstraint("intentos >= 0", name="ck_provisioning_requests_intentos"),
    )
    op.create_index(
        "uq_provisioning_requests_tenant_referencia",
        "provisioning_requests",
        ["tenant_id", "referencia"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )
    op.create_index(
        "uq_provisioning_requests_tenant_subscription",
        "provisioning_requests",
        ["tenant_id", "subscription_id"],
        unique=True,
        postgresql_where=sa.text("subscription_id is not null and deleted_at is null"),
    )
    op.create_index(
        "ix_provisioning_requests_tenant_estado",
        "provisioning_requests",
        ["tenant_id", "estado", "created_at"],
    )
    op.execute("ALTER TABLE provisioning_requests ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE provisioning_requests FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY provisioning_requests_tenant_isolation ON provisioning_requests
        USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid);
        """
    )
    # Explícito: los default privileges de 0003 solo valen si migra el mismo rol que la corrió.
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON provisioning_requests TO app_runtime;
            END IF;
        END $do$;
        """
    )

    op.add_column("tenant_invitations", sa.Column("token_nonce", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_invitations", "token_nonce")
    op.execute("drop policy if exists provisioning_requests_tenant_isolation on provisioning_requests")
    op.execute("alter table provisioning_requests no force row level security")
    op.execute("alter table provisioning_requests disable row level security")
    op.drop_index("ix_provisioning_requests_tenant_estado", table_name="provisioning_requests")
    op.drop_index("uq_provisioning_requests_tenant_subscription", table_name="provisioning_requests")
    op.drop_index("uq_provisioning_requests_tenant_referencia", table_name="provisioning_requests")
    op.drop_table("provisioning_requests")
