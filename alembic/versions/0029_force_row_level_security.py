"""FORCE ROW LEVEL SECURITY en toda tabla `public` con RLS habilitada.

Hallazgo real (checklist de promoción a producción): ninguna migración
anterior usaba `FORCE ROW LEVEL SECURITY` -- sin ella, el DUEÑO de la
tabla (el rol de migraciones, `agent_core`) puede saltarse las policies de
RLS por completo (Postgres exime a los dueños de tabla por default). Esto
no es un problema en el camino feliz (el runtime conecta como
`app_runtime`, no como el dueño), pero es una brecha real si algún código
o script futuro corriera con las credenciales de migración.

Se resuelve la lista de tablas DINÁMICAMENTE (`select ... from pg_tables
where rowsecurity=true`) en vez de enumerarlas a mano: ya hay 3
migraciones distintas (`0003`, `0015`, `0019`) que habilitaron RLS con
listas propias, más otras sueltas -- una lista fija acá se desactualizaría
en el próximo ticket que agregue una tabla con RLS.

Revision ID: 0029
Revises: 0028
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

# `pg_tables` no tiene columna `forcerowsecurity` -- hay que ir a
# `pg_class`/`pg_namespace` directo (`relrowsecurity`/`relforcerowsecurity`).
_SELECT_UNFORCED = """
    select c.relname from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
      and c.relrowsecurity = true and not c.relforcerowsecurity
"""
_SELECT_FORCED = """
    select c.relname from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
      and c.relrowsecurity = true and c.relforcerowsecurity
"""


def upgrade() -> None:
    conn = op.get_bind()
    tables = [row[0] for row in conn.execute(sa.text(_SELECT_UNFORCED))]
    for table in tables:
        conn.execute(sa.text(f'alter table "{table}" force row level security'))


def downgrade() -> None:
    conn = op.get_bind()
    tables = [row[0] for row in conn.execute(sa.text(_SELECT_FORCED))]
    for table in tables:
        conn.execute(sa.text(f'alter table "{table}" no force row level security'))
