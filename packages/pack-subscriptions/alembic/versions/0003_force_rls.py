"""Forzar RLS en las tablas de pack-subscriptions (N-36).

Las migraciones `0001`+`0002` habilitaron RLS pero no la forzaron: el dueño de la
tabla se saltaba la policy de aislamiento. Esta migración pone `FORCE ROW LEVEL
SECURITY` en las tablas vivas del pack (`plans` se eliminó en `0002`).

Revision ID: pack_subscriptions_0003
Revises: pack_subscriptions_0002
Create Date: 2026-10-02
"""

from __future__ import annotations

from alembic import op

revision = "pack_subscriptions_0003"
down_revision = "pack_subscriptions_0002"
branch_labels = None
depends_on = None

TABLES = (
    "pack_subscriptions_subscriptions",
    "pack_subscriptions_charges",
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"alter table {table} force row level security")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"alter table {table} no force row level security")
