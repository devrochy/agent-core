"""`app_settings.agent_defaults`: los valores por defecto de los agentes del negocio (ADR-0206 §6, HU-AOS-91 parte C).

Voz, tope de largo, frases prohibidas, modelo, memoria, políticas y playbooks base del negocio salen de `agent_specs`
(ADR-0191 §4 los dejaba ahí) y pasan a una columna JSONB de `app_settings`, con la forma que lee
`core/agents/defaults_negocio.py`. El backfill copia, por negocio, esas cinco partes del `agent_specs` activo: nada
cambia de valor. Un negocio sin spec activo queda con `{}` (los por defecto del código).

`app_settings` ya tiene `tenant_id`, RLS forzada y policy: la columna nueva no cambia el aislamiento. El UPDATE corre
con el rol de migraciones, que necesita BYPASSRLS (como 0147): sin él tocaría 0 filas en silencio.

Reversible: el downgrade quita la columna. Los valores siguen en `agent_specs` hasta la parte E (DROP de la tabla).

Revision ID: 0152
Revises: 0151
Create Date: 2026-11-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0152"
down_revision = "0151"
branch_labels = None
depends_on = None

_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"

_BACKFILL = """
update app_settings a
   set agent_defaults = jsonb_strip_nulls(jsonb_build_object(
         'persona', s.spec -> 'persona',
         'model', s.spec -> 'model',
         'memory', s.spec -> 'memory',
         'policies', s.spec -> 'policies',
         'playbooks', s.spec -> 'playbooks'))
  from agent_specs s
 where s.tenant_id = a.tenant_id and s.is_active and s.deleted_at is null and a.deleted_at is null and a.agent_defaults = '{}'::jsonb
"""


def upgrade() -> None:
    if not op.get_bind().execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError("0152: el rol de migraciones no puede saltar la RLS de app_settings; usa el dueño.")
    op.add_column(
        "app_settings",
        sa.Column(
            "agent_defaults",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(_BACKFILL)


def downgrade() -> None:
    op.drop_column("app_settings", "agent_defaults")
