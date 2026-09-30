"""FORCE ROW LEVEL SECURITY en las tablas de pack_cobranza -- mismo hallazgo y
criterio que `pack-payments/.../0002_force_row_level_security.py`: las
migraciones de packs corren después que las del core, así que la migración del
core no alcanza a cubrir estas tablas.

Revision ID: pack_cobranza_0003
Revises: pack_cobranza_0002
Create Date: 2026-09-24
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "pack_cobranza_0003"
down_revision = "pack_cobranza_0002"
branch_labels = None
depends_on = None

_SELECT_UNFORCED = """
    select c.relname from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
      and c.relname like 'pack_cobranza_%'
      and c.relrowsecurity = true and not c.relforcerowsecurity
"""
_SELECT_FORCED = """
    select c.relname from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
      and c.relname like 'pack_cobranza_%'
      and c.relrowsecurity = true and c.relforcerowsecurity
"""


def upgrade() -> None:
    conn = op.get_bind()
    for (table,) in conn.execute(sa.text(_SELECT_UNFORCED)):
        conn.execute(sa.text(f'alter table "{table}" force row level security'))


def downgrade() -> None:
    conn = op.get_bind()
    for (table,) in conn.execute(sa.text(_SELECT_FORCED)):
        conn.execute(sa.text(f'alter table "{table}" no force row level security'))
