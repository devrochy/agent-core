"""Enrutamiento entre especialistas y su modo sombra (HU-AOS-17, ADR-0138).

1. `tenants.aos_routing` (`off` | `shadow` | `active`, por defecto `off`): bandera del master por
   negocio, como `aos_runtime` y `aos_procedures` (ADR-0112). Con `off` el turno no enruta (el
   foco es el de siempre: dueño → por defecto). El CHECK de orden exige `aos_runtime = 'v3'` para
   encenderla. `tenants` es chica: los CHECK entran validados.
2. `core_decisions.shadow_route jsonb`: la ruta que el Coordinador habría tomado en modo sombra
   (`{agent_key, instance_id, route_source, rule_id, procedure_id}`, sin texto del cliente). La
   ruta real sigue en `route_source`. CHECK gemelo de `ck_core_decisions_routed`: solo en turnos
   `routed` y siempre un objeto. `NOT VALID` (la tabla es grande y append-only; la columna nace
   vacía), se valida con el contract como las demás de 0070.

**Orden del rollback:** primero el código (banderas.py lee `aos_routing` en cada turno y
decisions.py escribe `shadow_route`), después el `downgrade`, que además borra el historial de
`shadow_route`. La revisión se re-encadena sobre el head real de develop al mergear (0090–0093 están
reservadas para PRs en paralelo).

Columnas con default constante o nullables: sin reescritura. Cada tabla en su propia transacción
con `lock_timeout` de 5 s (patrón de 0070). Re-ejecutable.

Revision ID: 0094
Revises: 0090
Create Date: 2026-10-18
"""

from __future__ import annotations

from alembic import op

revision = "0094"
down_revision = "0090"
branch_labels = None
depends_on = None

_TENANTS = """
DO $$
BEGIN
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS aos_routing text NOT NULL DEFAULT 'off';
IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tenants_aos_routing'
               AND conrelid = 'public.tenants'::regclass) THEN
  ALTER TABLE tenants ADD CONSTRAINT ck_tenants_aos_routing
    CHECK (aos_routing IN ('off','shadow','active'));
END IF;
IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tenants_aos_routing_order'
               AND conrelid = 'public.tenants'::regclass) THEN
  ALTER TABLE tenants ADD CONSTRAINT ck_tenants_aos_routing_order
    CHECK (aos_routing = 'off' OR aos_runtime = 'v3');
END IF;
END $$;
"""

_DECISIONS = """
DO $$
BEGIN
ALTER TABLE core_decisions ADD COLUMN IF NOT EXISTS shadow_route jsonb;
IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_core_decisions_shadow'
               AND conrelid = 'public.core_decisions'::regclass) THEN
  ALTER TABLE core_decisions ADD CONSTRAINT ck_core_decisions_shadow
    CHECK (shadow_route IS NULL OR (outcome = 'routed' AND jsonb_typeof(shadow_route) = 'object')) NOT VALID;
END IF;
END $$;
"""


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            op.execute(_DECISIONS)
            op.execute(_TENANTS)
        finally:
            op.execute("RESET lock_timeout")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            _bajar()
        finally:
            op.execute("RESET lock_timeout")


def _bajar() -> None:
    op.execute(
        "ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenants_aos_routing_order, "
        "DROP CONSTRAINT IF EXISTS ck_tenants_aos_routing, DROP COLUMN IF EXISTS aos_routing"
    )
    op.execute(
        "ALTER TABLE core_decisions DROP CONSTRAINT IF EXISTS ck_core_decisions_shadow, "
        "DROP COLUMN IF EXISTS shadow_route"
    )
