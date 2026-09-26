"""admin_tenants(): un master ve todos los tenants activos (ADR-0047).

`admin_tenants()` (ADR-0047 §4) hace que un admin con algún permiso global
(`tenant_id is null`) vea todos los tenants activos. Un `master`, en cambio,
solo veía los de `tenant_user`, así que su selector no incluía tenants sin
vínculo. Esta migración reescribe la función para que `Admin.role = 'master'`
vea **todos** los tenants activos, con el mismo criterio: prefiriendo el rol
real de `tenant_user` cuando existe, y `'global'` cuando no.

No cambia el acceso a datos: el master ya tenía bypass total de secciones
(`effective_access`, ADR-0047 §3); esto solo amplía el conjunto de tenants
seleccionables por el middleware (ADR-0012).

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-26
"""
from __future__ import annotations

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
                union all
                select t.id as tenant_id, t.name as tenant_name, 'global' as role, 2 as prio
                from tenants t
                where t.status = 'active'
                  and exists (
                      select 1 from admins a
                      where a.id = p_admin_id and a.role = 'master'
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
