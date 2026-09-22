"""RBAC del panel: permisos por sección y alcance de tenants (ADR-0047).

Nueva tabla de SISTEMA `admin_permissions` (sin `tenant_id not null`, sin RLS):
se consulta al autenticar, antes de conocer el tenant, igual que `admins`,
`admin_sessions` y `tenant_user` (ADR-0012). `tenant_id` nullable: `null` =
el permiso vale para todos los tenants (global), con valor = solo ese tenant.

Se reescribe `admin_tenants()` (patrón de 0008) para que un admin con algún
permiso global vea todos los tenants activos, además de los que tenga por
`tenant_user`; sin esto, un admin de marketing no obtendría tenant activo en
el middleware. No hay backfill: `master`/`owner`/`colaborador` siguen valiendo
como bypass y valores por defecto (ver ADR-0047).

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-22
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    op.create_table(
        "admin_permissions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", UUID(as_uuid=True), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("access", sa.Text(), nullable=False, server_default="read"),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("access in ('read', 'write')", name="ck_admin_permissions_access"),
    )
    op.create_index("ix_admin_permissions_admin", "admin_permissions", ["admin_id"])
    # Un permiso por (admin, sección, alcance). `tenant_id` null = global, así que
    # el índice único usa `coalesce` (Postgres trata los NULL como distintos).
    op.execute(
        "create unique index uq_admin_permission_scope on admin_permissions "
        f"(admin_id, section, coalesce(tenant_id, '{_ZERO_UUID}'::uuid))"
    )
    op.execute("grant select, insert, update, delete on admin_permissions to app_runtime")

    # `admin_tenants`: tenants de `tenant_user` + (si hay permiso global) todos los
    # activos, un registro por tenant, prefiriendo el rol real de `tenant_user`.
    op.execute("drop function if exists admin_tenants(uuid)")
    op.execute(
        """
        create function admin_tenants(p_admin_id uuid)
        returns table(tenant_id uuid, tenant_name text, role text)
        language sql security definer set search_path = public as $$
            select distinct on (tenant_id) tenant_id, tenant_name, role
            from (
                select tu.tenant_id as tenant_id, t.name as tenant_name, tu.role as role, 0 as prio
                from tenant_user tu
                join tenants t on t.id = tu.tenant_id
                where tu.admin_id = p_admin_id
                union all
                select t.id as tenant_id, t.name as tenant_name, 'global' as role, 1 as prio
                from tenants t
                where t.status = 'active'
                  and exists (
                      select 1 from admin_permissions ap
                      where ap.admin_id = p_admin_id and ap.tenant_id is null
                  )
            ) s
            order by tenant_id, prio;
        $$;
        """
    )
    op.execute("grant execute on function admin_tenants(uuid) to app_runtime")


def downgrade() -> None:
    op.execute("drop function if exists admin_tenants(uuid)")
    op.execute(
        """
        create function admin_tenants(p_admin_id uuid)
        returns table(tenant_id uuid, tenant_name text, role text)
        language sql security definer set search_path = public as $$
            select tu.tenant_id, t.name, tu.role
            from tenant_user tu join tenants t on t.id = tu.tenant_id
            where tu.admin_id = p_admin_id;
        $$;
        """
    )
    op.execute("drop index if exists uq_admin_permission_scope")
    op.execute("drop index if exists ix_admin_permissions_admin")
    op.drop_table("admin_permissions")
