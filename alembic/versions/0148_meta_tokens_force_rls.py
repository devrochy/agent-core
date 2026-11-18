"""FORCE ROW LEVEL SECURITY en `meta_private.meta_tokens`.

La `0018` le habilitó RLS sin FORCE, y la `0029` solo forzó las tablas del schema `public`: el dueño de la tabla (el
rol de migraciones) leía los tokens cifrados sin pasar por la policy `tenant_isolation`. Incumplía la invariante 1 y
hacía fallar `scripts/verificar_aislamiento.py` en `rls_otros_esquemas` (fase 0 de `docs/runbooks/produccion-vps.md`).

No cambia nada para el runtime (`app_runtime` no es dueño, la RLS ya le aplicaba). Las migraciones corren con un dueño
`BYPASSRLS`, al que FORCE no le afecta. Sin cambio de esquema ni de datos; `core/db/models.py` no modela FORCE.

Reversible: el downgrade deja la tabla como la dejó la `0018` (`no force`).

Revision ID: 0148
Revises: 0147
Create Date: 2026-11-18
"""

from __future__ import annotations

from alembic import op

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table meta_private.meta_tokens force row level security")


def downgrade() -> None:
    op.execute("alter table meta_private.meta_tokens no force row level security")
