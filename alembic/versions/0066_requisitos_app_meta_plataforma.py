"""Requisitos de Meta de la app de la plataforma para Embedded Signup (HU-SH-05, ADR-0128).

`platform_meta_app_requirements`: lo que el admin de plataforma confirma a mano en "Plataforma
→ App de Meta" (verificación del negocio, App Review de los permisos, acceso de Tech Provider,
configuración de Embedded Signup y app en modo Live), con quién y cuándo. Meta no expone esos
estados de forma fiable a la app: se confirman con el enlace a cada pantalla.

Tabla de SISTEMA (sin RLS, sin `tenant_id`): los requisitos son de la app de Meta de la
plataforma, compartida por todos los negocios, no de un negocio (mismo criterio que
`meta_app_subscriptions`, ADR-0029). Solo la escribe `core/admin/platform_meta_es.py`, que
exige el rol master. Sin `DELETE` para `app_runtime`: desmarcar es `done = false`.

Tabla nueva y vacía: sin reescritura ni bloqueo largo.

Revision ID: 0066
Revises: 0063
Create Date: 2026-10-09
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0066"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_meta_app_requirements",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "updated_by",
            UUID(as_uuid=True),
            sa.ForeignKey("admins.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("char_length(key) <= 64", name="ck_platform_meta_app_requirements_key"),
        sa.CheckConstraint("notes is null or char_length(notes) <= 500", name="ck_platform_meta_app_requirements_notes"),
    )
    op.create_index(
        "ix_platform_meta_app_requirements_updated_by", "platform_meta_app_requirements", ["updated_by"]
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                GRANT SELECT, INSERT, UPDATE ON platform_meta_app_requirements TO app_runtime;
                -- Los default privileges de 0003 dan también DELETE: se quita (invariante 8).
                REVOKE DELETE, TRUNCATE ON platform_meta_app_requirements FROM app_runtime;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index("ix_platform_meta_app_requirements_updated_by", table_name="platform_meta_app_requirements")
    op.drop_table("platform_meta_app_requirements")
