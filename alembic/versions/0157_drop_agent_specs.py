"""Se borra `agent_specs` y la columna `agent_versions.legacy_agent_spec_id` (ADR-0206 §9, HU-AOS-91 parte E).

Desde la parte D3 ningún código lee ni escribe el AgentSpec v2: el turno corre sobre `AgentSpecV3`/`KernelSpec`, las
habilidades del negocio son las instaladas en sus agentes (`pack_installations`) y los valores por defecto viven en
`app_settings.agent_defaults` (0152). Retirar por ADR un esquema que ningún código lee no es el borrado físico de filas de
negocio que prohíbe la invariante 8; ADR-0206 §9 lo registra explícitamente.

Guarda (decisión del dueño del 2026-11-22): antes del DROP se compara, por negocio con Coordinador, `agent_specs.packs` del
spec activo con las habilidades instaladas en sus agentes activos. Si algún negocio perdería una habilidad, la migración **aborta** y la
lista, salvo que el entorno traiga `AGENT_SPECS_DROP_ACEPTADO=1` (quien despliega revisó y acepta esas pérdidas). Así, una
release que llega con C2 y esta migración juntas no borra nada sin que alguien lo mire. Respaldo de la base antes de correrla.

Requiere el rol de migraciones con BYPASSRLS (como 0152): la guarda lee todos los negocios.

`downgrade()` recrea la tabla y la columna VACÍAS (los datos no vuelven: se restauran desde el respaldo).

Revision ID: 0157
Revises: 0156
Create Date: 2026-11-22
"""

from __future__ import annotations

import os

import sqlalchemy as sa

from alembic import op

revision = "0157"
down_revision = "0156"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"
_ACEPTADO = "AGENT_SPECS_DROP_ACEPTADO"

# Por negocio vivo del Agent OS (con Coordinador): los packs del spec v2 activo (sin `core`) que ningún agente activo tiene
# instalados. Un negocio sin Coordinador no tiene agentes, así que desde C2 ya no tenía habilidades: no hay nada que
# proteger (es el `tenant-1` que siembran las migraciones viejas en una base nueva, la del CI).
_FALTANTES = """
select t.slug, array_agg(distinct p order by p)
  from agent_specs s
  join tenants t on t.id = s.tenant_id and t.deleted_at is null
  cross join lateral jsonb_array_elements_text(coalesce(s.spec -> 'packs', '[]'::jsonb)) as p
 where s.is_active and s.deleted_at is null and p <> 'core'
   and exists (
       select 1 from agents c
        where c.tenant_id = s.tenant_id and c.kind = 'core' and c.deleted_at is null
   )
   and not exists (
       select 1 from pack_installations i
         join agents a on a.tenant_id = i.tenant_id and a.id = i.agent_id
        where i.tenant_id = s.tenant_id and i.scope = 'agent' and i.pack_id = p
          and i.deleted_at is null and i.status in ('active', 'pending_config')
          and a.deleted_at is null and a.status = 'active'
   )
 group by t.slug
 order by t.slug
"""


def _guarda() -> None:
    bind = op.get_bind()
    if not bind.execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError("0157: el rol de migraciones no puede saltar la RLS de agent_specs; usa el dueño.")
    faltantes = bind.execute(sa.text(_FALTANTES)).all()
    if not faltantes:
        return
    detalle = "; ".join(f"{slug}: {', '.join(packs)}" for slug, packs in faltantes)
    if os.environ.get(_ACEPTADO) == "1":
        print(f"0157: {_ACEPTADO}=1, se aceptan las habilidades sin agente: {detalle}")
        return
    raise RuntimeError(
        f"0157: estos negocios perderían habilidades que hoy solo están en agent_specs: {detalle}. "
        f"Instálalas en un agente (`agent-core db packs-sin-instalacion --todos --aplicar`) o, si se aceptan, "
        f"corre la migración con {_ACEPTADO}=1."
    )


def upgrade() -> None:
    _guarda()
    op.drop_constraint("agent_versions_tenant_id_legacy_agent_spec_id_fkey", "agent_versions", type_="foreignkey")
    op.drop_index("uq_agent_versions_legacy", table_name="agent_versions")
    op.drop_column("agent_versions", "legacy_agent_spec_id")
    op.execute("drop table agent_specs")


def downgrade() -> None:
    op.execute(
        """
        create table agent_specs (
            id uuid not null primary key,
            tenant_id uuid not null,
            version text not null,
            is_active boolean not null default false,
            spec jsonb not null,
            deleted_at timestamptz,
            created_at timestamptz not null default now(),
            updated_at timestamptz not null default now(),
            constraint uq_agent_specs_tenant_id unique (tenant_id, id)
        )
        """
    )
    op.execute("create index ix_agent_specs_tenant_created on agent_specs (tenant_id, created_at)")
    op.execute(
        "create unique index uq_agent_specs_active on agent_specs (tenant_id) where is_active and deleted_at is null"
    )
    op.execute(
        "create unique index uq_agent_specs_tenant_version on agent_specs (tenant_id, version) where deleted_at is null"
    )
    op.execute("alter table agent_specs enable row level security")
    op.execute("alter table agent_specs force row level security")
    op.execute(f"create policy tenant_isolation on agent_specs using (tenant_id = {RLS}) with check (tenant_id = {RLS})")
    op.execute("grant select, insert, update, delete on agent_specs to app_runtime")
    op.add_column("agent_versions", sa.Column("legacy_agent_spec_id", sa.dialects.postgresql.UUID(as_uuid=True)))
    op.execute(
        "create unique index uq_agent_versions_legacy on agent_versions (tenant_id, agent_id, legacy_agent_spec_id) "
        "where legacy_agent_spec_id is not null"
    )
    op.execute(
        "alter table agent_versions add constraint agent_versions_tenant_id_legacy_agent_spec_id_fkey "
        "foreign key (tenant_id, legacy_agent_spec_id) references agent_specs (tenant_id, id)"
    )
