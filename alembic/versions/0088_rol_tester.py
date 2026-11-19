"""Rol tester del centro de pruebas (SPEC-30, HU-QA-04, ADR-0134).

1. `admin_tenants()` (patrón de 0037): un admin `tester` ve todos los negocios activos,
   igual que un `master`. El tester actúa como master salvo Colaboradores y solo con
   `QA_TESTERS_ENABLED` (test): sin la variable no inicia sesión, así que en producción
   la función nunca se llama con un tester activo.
2. `qa_tester_invitations`: enlace de un solo uso para que un tester defina su contraseña.

Tabla de SISTEMA (sin RLS): `qa_tester_invitations` es identidad de plataforma, como
`password_reset_tokens` (ADR-0098); no pertenece a ningún negocio y se consulta sin
contexto de tenant (el invitado todavía no tiene sesión). Grants mínimos a app_runtime
(ADR-0133, ADR-0134).

Revision ID: 0088
Revises: 0087
Create Date: 2026-10-13
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def _admin_tenants(roles_de_plataforma: str) -> str:
    return f"""
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
                      where a.id = p_admin_id and a.role in ({roles_de_plataforma})
                  )
            ) s
            order by tenant_id, prio;
        $$;
    """


def upgrade() -> None:
    op.execute("drop function if exists admin_tenants(uuid)")
    op.execute(_admin_tenants("'master', 'tester'"))
    op.execute("grant execute on function admin_tenants(uuid) to app_runtime")

    op.create_table(
        "qa_tester_invitations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("admin_id", UUID(as_uuid=True), sa.ForeignKey("admins.id"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("token_nonce", sa.Text()),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invitado_por", UUID(as_uuid=True), sa.ForeignKey("admins.id")),
        sa.Column("creada_en", sa.DateTime(timezone=True), nullable=False),
        sa.Column("respondida_en", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "estado in ('pendiente', 'aceptada', 'revocada', 'reemplazada')",
            name="ck_qa_tester_invitations_estado",
        ),
    )
    op.create_index("ix_qa_tester_invitations_admin", "qa_tester_invitations", ["admin_id"])
    op.execute("grant select, insert, update, delete on qa_tester_invitations to app_runtime")


def downgrade() -> None:
    op.drop_index("ix_qa_tester_invitations_admin", table_name="qa_tester_invitations")
    op.drop_table("qa_tester_invitations")
    op.execute("drop function if exists admin_tenants(uuid)")
    op.execute(_admin_tenants("'master'"))
    op.execute("grant execute on function admin_tenants(uuid) to app_runtime")
