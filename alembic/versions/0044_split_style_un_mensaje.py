"""Split style por defecto: un solo mensaje (HU-ARCH-11).

Desde 2026-10-01 Meta cobra cada mensaje de servicio. El default pasa de
`pocos_cortos` (hasta 3 burbujas por turno) a `un_mensaje`.

Esta migración **fija explícitamente** `split_style = 'un_mensaje'` en los
negocios que NO lo eligieron (no tienen la clave en `behavior_config`), para
que la decisión quede auditada en datos y no dependa del default del código.
Los negocios que ya lo eligieron (tienen la clave) se respetan tal cual.

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-02
"""
from __future__ import annotations

from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        update app_settings
        set behavior_config = coalesce(behavior_config, '{}'::jsonb)
            || '{"split_style": "un_mensaje"}'::jsonb
        where coalesce(behavior_config, '{}'::jsonb) ? 'split_style' = false
        """
    )


def downgrade() -> None:
    # Solo se revierte la clave que esta migración pudo haber fijado; una
    # elección explícita previa (distinta de un_mensaje) se deja intacta.
    op.execute(
        """
        update app_settings
        set behavior_config = behavior_config - 'split_style'
        where behavior_config->>'split_style' = 'un_mensaje'
        """
    )
