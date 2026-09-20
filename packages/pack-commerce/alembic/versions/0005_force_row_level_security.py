"""FORCE ROW LEVEL SECURITY en las tablas de pack_commerce -- hallazgo real
de la checklist de promoción a producción de agent-core (`core/gateway/
meta_production_readiness.py::check_rls_forced`): las migraciones de
pack_commerce corren DESPUÉS de las del core (`agent-core db migrate`),
así que la migración equivalente del core (`0029_force_row_level_security`)
no alcanza a cubrir estas tablas -- se necesita el mismo fix acá.

Mismo criterio que la migración del core: se resuelve la lista de tablas
DINÁMICAMENTE (`select ... from pg_class/pg_namespace where
relrowsecurity=true`) en vez de enumerarlas a mano, para no desactualizarse
en el próximo cambio de esquema del pack.

Revision ID: crm_0005
Revises: crm_0004
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "crm_0005"
down_revision = "crm_0004"
branch_labels = None
depends_on = None

_SELECT_UNFORCED = """
    select c.relname from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
      and c.relname like 'pack_commerce_%'
      and c.relrowsecurity = true and not c.relforcerowsecurity
"""
_SELECT_FORCED = """
    select c.relname from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
      and c.relname like 'pack_commerce_%'
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
