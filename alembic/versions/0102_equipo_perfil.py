"""Perfil del colaborador: nombre, teléfono y avatar (sección Equipo).

La sección Equipo deja de mostrar solo `email`/`username`: cada cuenta lleva un nombre
completo, un teléfono (con indicativo) y una foto de perfil. La foto se edita desde el
perfil del propio usuario (funcionalidad posterior); aquí se guarda la columna y el panel
muestra iniciales mientras no haya `avatar_url`.

**Rollback:** solo el `downgrade`; no hay datos que migrar.

Revision ID: 0102
Revises: 0101
Create Date: 2026-10-21
"""

from __future__ import annotations

from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS nombre text")
    op.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS telefono text")
    op.execute("ALTER TABLE admins ADD COLUMN IF NOT EXISTS avatar_url text")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE admins DROP COLUMN IF EXISTS avatar_url")
    op.execute("ALTER TABLE admins DROP COLUMN IF EXISTS telefono")
    op.execute("ALTER TABLE admins DROP COLUMN IF EXISTS nombre")
