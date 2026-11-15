"""Datos del cliente por agente (HU-CRM-28, enmienda de ADR-0179).

La política de datos del cliente deja de vivir en `app_settings.behavior_config` y pasa a
`agents.data_policy`: `{politica: {accion: {clave: nivel}}, herramientas: bool}`. Solo guarda lo
que difiere del valor por defecto del módulo.

La política global se migra a cada especialista con `scripts/migrar_datos_cliente_agentes.py`
(`--dry-run` por defecto) antes de retirarla. Esta migración solo crea la columna: no toca datos.

Revision ID: 0134
Revises: 0133
Create Date: 2026-11-14
"""

from __future__ import annotations

from alembic import op

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE agents
        ADD COLUMN data_policy jsonb NOT NULL DEFAULT '{}'::jsonb
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS data_policy")
