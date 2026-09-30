"""Invitaciones de membresía a un tenant (ADR-0081).

Tabla de SISTEMA `tenant_invitations` (sin RLS): el invitado debe poder ver y
aceptar/rechazar invitaciones ANTES de tener acceso al tenant, igual que
`tenant_user` y `admin_permissions` (ADR-0012). El invitado se identifica por
`admin_id` (identidad existente; los usuarios nuevos se vinculan directo).

Estados: `pendiente | aceptada | rechazada | revocada`. Índice único parcial: una
invitación pendiente por `(tenant_id, admin_id)`.

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_invitations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("admin_id", UUID(as_uuid=True), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("invited_by", UUID(as_uuid=True), sa.ForeignKey("admins.id"), nullable=True),
        sa.Column("role", sa.Text(), nullable=False, server_default="colaborador"),
        sa.Column("pack_roles", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "estado in ('pendiente', 'aceptada', 'rechazada', 'revocada')",
            name="ck_tenant_invitations_estado",
        ),
        sa.CheckConstraint(
            "role in ('owner', 'colaborador')",
            name="ck_tenant_invitations_role",
        ),
    )
    op.create_index("ix_tenant_invitations_admin", "tenant_invitations", ["admin_id"])
    op.execute(
        "create unique index uq_tenant_invitation_pending on tenant_invitations "
        "(tenant_id, admin_id) where estado = 'pendiente'"
    )
    op.execute("grant select, insert, update, delete on tenant_invitations to app_runtime")


def downgrade() -> None:
    op.drop_index("uq_tenant_invitation_pending", table_name="tenant_invitations")
    op.drop_index("ix_tenant_invitations_admin", table_name="tenant_invitations")
    op.drop_table("tenant_invitations")
