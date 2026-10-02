"""Migrar AgentSpecs con modelos DeepSeek discontinuados (HU-ARCH-10, P-05).

`deepseek-chat` y `deepseek-reasoner` se discontinuaron el 2026-07-24. Los specs
vivos que los referencian fallarían por completo. Esta migración reescribe el
JSONB `agent_specs.spec` para usar el modelo vigente (`deepseek-flash`) en
`model.primary`, `model.router` y `model.fallback`, dejando trazas auditables.

Es idempotente y no toca specs que ya usan un ID vigente.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-02
"""
from __future__ import annotations

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

NEW_MODEL = "deepseek-flash"
RETIRED = ("deepseek-chat", "deepseek-reasoner")


def upgrade() -> None:
    # `jsonb_set` por cada clave de `model` que apunte a un ID retirado. Se hace
    # en SQL (no en Python) porque puede haber varias versiones por tenant.
    op.execute(
        f"""
        update agent_specs
        set spec = jsonb_set(
            jsonb_set(
                jsonb_set(
                    spec,
                    '{{model,primary}}',
                    to_jsonb(case
                        when coalesce(spec->'model'->>'primary', '') in ('{RETIRED[0]}', '{RETIRED[1]}')
                        then '{NEW_MODEL}'
                        else spec->'model'->>'primary' end),
                    true
                ),
                '{{model,router}}',
                to_jsonb(case
                    when coalesce(spec->'model'->>'router', '') in ('{RETIRED[0]}', '{RETIRED[1]}')
                    then '{NEW_MODEL}'
                    else coalesce(spec->'model'->>'router', '') end),
                true
            ),
            '{{model,fallback}}',
            to_jsonb(case
                when coalesce(spec->'model'->>'fallback', '') in ('{RETIRED[0]}', '{RETIRED[1]}')
                then '{NEW_MODEL}'
                else coalesce(spec->'model'->>'fallback', '') end),
            true
        ),
        updated_at = now()
        where spec->'model'->>'primary' in ('{RETIRED[0]}', '{RETIRED[1]}')
           or spec->'model'->>'router' in ('{RETIRED[0]}', '{RETIRED[1]}')
           or spec->'model'->>'fallback' in ('{RETIRED[0]}', '{RETIRED[1]}')
        """
    )


def downgrade() -> None:
    # Sin reversa: volver a un ID discontinuado no tiene sentido. Se deja el
    # modelo vigente (la migración es de datos, no de esquema).
    pass
