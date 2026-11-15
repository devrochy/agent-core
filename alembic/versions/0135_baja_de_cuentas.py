"""Baja lógica de cuentas de equipo (ADR-0198).

`admins` gana las columnas de la baja lógica. La fila se conserva: el historial de cartera
guarda el correo como texto (`registrado_por`) y la auditoría necesita la identidad real, así
que no se anonimiza ni se reescribe el correo. `deleted_from_tenant_id` solo se llena cuando
la baja la hizo un dueño/tester: ese negocio puede reactivar la cuenta.

Revision ID: 0135
Revises: 0134
Create Date: 2026-11-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0135"
down_revision = "0134"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("admins", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("admins", sa.Column("deleted_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "admins",
        sa.Column("deleted_from_tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "admins",
        sa.Column("deleted_links", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.add_column("admins", sa.Column("deletion_reason", sa.Text(), nullable=True))
    op.create_index(
        "ix_admins_deleted_from_tenant",
        "admins",
        ["deleted_from_tenant_id"],
        unique=False,
        postgresql_where=sa.text("deleted_at is not null"),
    )


def downgrade() -> None:
    op.drop_index("ix_admins_deleted_from_tenant", table_name="admins")
    op.drop_column("admins", "deletion_reason")
    op.drop_column("admins", "deleted_links")
    op.drop_column("admins", "deleted_from_tenant_id")
    op.drop_column("admins", "deleted_by")
    op.drop_column("admins", "deleted_at")
