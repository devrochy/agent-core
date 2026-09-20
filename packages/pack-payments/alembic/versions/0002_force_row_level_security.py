"""FORCE ROW LEVEL SECURITY en las tablas de pack_payments -- mismo
hallazgo/criterio que `pack-commerce/alembic/versions/0005_force_row_
level_security.py` y `core/alembic/versions/0029_force_row_level_security.py`:
las migraciones de packs corren después que las del core, así que la
migración del core no alcanza a cubrir estas tablas.

Revision ID: pack_payments_0002
Revises: pack_payments_0001
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "pack_payments_0002"
down_revision = "pack_payments_0001"
branch_labels = None
depends_on = None

_SELECT_UNFORCED = """
    select c.relname from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
      and c.relname like 'pack_payments_%'
      and c.relrowsecurity = true and not c.relforcerowsecurity
"""
_SELECT_FORCED = """
    select c.relname from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = 'public' and c.relkind = 'r'
      and c.relname like 'pack_payments_%'
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
