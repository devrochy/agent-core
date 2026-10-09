"""Hitos del onboarding: el embudo de activación (HU-SH-06, ADR-0124).

- `onboarding_hitos`: la primera vez que un negocio completó cada paso de los primeros pasos
  (y los bordes del recorrido: invitación aceptada y primer mensaje real), con los segundos desde
  el alta. Tabla de NEGOCIO (`tenant_id`, RLS habilitada y forzada, policy de aislamiento): una
  fila por `(tenant_id, paso)` mientras no esté borrada. El estado de cada paso NO se guarda: se
  calcula del estado real; esta tabla solo fija cuándo se cumplió por primera vez.

Tabla nueva y vacía: sin reescritura ni bloqueo largo. `purge_tenant` la cubre sola (descubre las
tablas con `tenant_id`).

Revision ID: 0060
Revises: 0059
Create Date: 2026-10-09
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_hitos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("paso", sa.Text(), nullable=False),
        sa.Column("completado_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("segundos_desde_alta", sa.BigInteger(), nullable=False),
        sa.Column("fuente", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("paso ~ '^[a-z][a-z0-9_]{0,63}$'", name="ck_onboarding_hitos_paso"),
        sa.CheckConstraint("segundos_desde_alta >= 0", name="ck_onboarding_hitos_segundos"),
        sa.CheckConstraint("fuente in ('dato', 'deteccion')", name="ck_onboarding_hitos_fuente"),
    )
    op.create_index(
        "uq_onboarding_hitos_tenant_paso",
        "onboarding_hitos",
        ["tenant_id", "paso"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )
    op.execute("ALTER TABLE onboarding_hitos ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE onboarding_hitos FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY onboarding_hitos_tenant_isolation ON onboarding_hitos
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
                GRANT SELECT, INSERT, UPDATE, DELETE ON onboarding_hitos TO app_runtime;
            END IF;
        END $do$;
        """
    )


def downgrade() -> None:
    op.execute("drop policy if exists onboarding_hitos_tenant_isolation on onboarding_hitos")
    op.execute("alter table onboarding_hitos no force row level security")
    op.execute("alter table onboarding_hitos disable row level security")
    op.drop_index("uq_onboarding_hitos_tenant_paso", table_name="onboarding_hitos")
    op.drop_table("onboarding_hitos")
