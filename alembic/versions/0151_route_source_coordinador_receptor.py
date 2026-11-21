"""`core_decisions.route_source` acepta `coordinador_receptor` (ADR-0205 §3, HU-AOS-88).

Sin especialistas en la release, el foco del turno es el Coordinador y la ruta se anota con la fuente
`coordinador_receptor`. El CHECK en línea de 0069 (`core_decisions_route_source_check`) solo aceptaba
`task`, `rule`, `trigger`, `coprocessor` y `default`: el INSERT de la decisión fallaría en cada turno.

Se cambia el CHECK con `NOT VALID` y luego `VALIDATE CONSTRAINT`: el ADD solo toma un lock breve, y la
validación recorre la tabla sin bloquear escrituras (las filas viejas cumplen las dos versiones). Sin
cambios de RLS: `core_decisions` ya tiene `tenant_id`, RLS forzada y policy.

Reversible: el downgrade pasa a `default` las filas con `coordinador_receptor` (es lo más cercano: el foco
por defecto de la release) y repone el CHECK de 0069.

Revision ID: 0151
Revises: 0150
Create Date: 2026-11-21
"""

from __future__ import annotations

from alembic import op

revision = "0151"
down_revision = "0150"
branch_labels = None
depends_on = None

_NOMBRE = "core_decisions_route_source_check"
_VIEJAS = "'task','rule','trigger','coprocessor','default'"
_NUEVAS = _VIEJAS + ",'coordinador_receptor'"


def _cambiar(fuentes: str) -> None:
    op.execute(f"ALTER TABLE core_decisions DROP CONSTRAINT IF EXISTS {_NOMBRE}")
    op.execute(
        f"ALTER TABLE core_decisions ADD CONSTRAINT {_NOMBRE} "
        f"CHECK (route_source IN ({fuentes})) NOT VALID"
    )
    op.execute(f"ALTER TABLE core_decisions VALIDATE CONSTRAINT {_NOMBRE}")


def upgrade() -> None:
    _cambiar(_NUEVAS)


def downgrade() -> None:
    op.execute("UPDATE core_decisions SET route_source = 'default' WHERE route_source = 'coordinador_receptor'")
    _cambiar(_VIEJAS)
