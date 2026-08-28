"""admin_tenants devuelve también el nombre del tenant (selector de tenant, C3).

Sin el nombre, el dropdown del panel no podría listar los tenants del admin sin
otra query. Se mantiene SECURITY DEFINER (ADR-0012): tabla de sistema, exenta de
RLS.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-28
"""
from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("drop function if exists admin_tenants(uuid)")
    op.execute(
        """
        CREATE FUNCTION admin_tenants(p_admin_id uuid)
        RETURNS TABLE(tenant_id uuid, tenant_name text, role text)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT tu.tenant_id, t.name, tu.role
            FROM tenant_user tu JOIN tenants t ON t.id = tu.tenant_id
            WHERE tu.admin_id = p_admin_id;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("drop function if exists admin_tenants(uuid)")
    op.execute(
        """
        CREATE FUNCTION admin_tenants(p_admin_id uuid)
        RETURNS TABLE(tenant_id uuid, role text)
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
            SELECT tu.tenant_id, tu.role FROM tenant_user tu WHERE tu.admin_id = p_admin_id;
        $$;
        """
    )