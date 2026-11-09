"""La deduplicación de la cola offline es por cuenta, no por negocio (agent-core#705).

`pack_cobranza_sync_eventos` era único por `(tenant_id, evento_id_cliente)`, y ese id lo genera cada
app: un gestor que repitiera el id de otro recibía «duplicado» (supo que existe y su evento se perdía).

- `actor`: la cuenta que mandó el evento (`admin_id` del token). Texto `NOT NULL` con default `''`, para
  que los tokens de dispositivo (sin cuenta) y las filas de antes sigan en una misma clave; un `NULL`
  no choca en un índice único. Columna aditiva con default constante: no reescribe la tabla.
- Índice único nuevo `(tenant_id, actor, evento_id_cliente)`, y se retira el anterior.

Las filas de antes quedan con `''`. La API de campo las sigue contando como ya aplicadas para cualquier
cuenta: un evento de la cola que se reintente tras desplegar no se aplica dos veces.

Compatible con el código anterior: al no enviar `actor`, sigue escribiendo `''` y su deduplicación no cambia.

El DDL es idempotente (`IF [NOT] EXISTS`): se puede volver a pasar tras un `stamp` a una revisión anterior.

Downgrade: el índice anterior no admite el mismo id de dos cuentas. La fila más vieja conserva el id y las
demás quedan como `<id>:<actor>`: no se borran eventos de cobro, y esa versión tampoco los volvería a aplicar.

Revision ID: pack_cobranza_0019
Revises: pack_cobranza_0018
Create Date: 2026-11-08
"""

from __future__ import annotations

from alembic import op

revision = "pack_cobranza_0019"
down_revision = "pack_cobranza_0018"
branch_labels = None
depends_on = None

_TABLA = "pack_cobranza_sync_eventos"
_INDICE_NUEVO = "uq_pack_cobranza_sync_evento_actor"
_INDICE_VIEJO = "uq_pack_cobranza_sync_evento"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"ALTER TABLE {_TABLA} ADD COLUMN IF NOT EXISTS actor text NOT NULL DEFAULT ''")
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDICE_NUEVO} ON {_TABLA} (tenant_id, actor, evento_id_cliente)")
    op.execute(f"DROP INDEX IF EXISTS {_INDICE_VIEJO}")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        f"""
        UPDATE {_TABLA} e
        SET evento_id_cliente = e.evento_id_cliente || ':' || e.actor
        FROM (
            SELECT id, row_number() OVER (PARTITION BY tenant_id, evento_id_cliente ORDER BY created_at, id) AS n
            FROM {_TABLA}
        ) d
        WHERE e.id = d.id AND d.n > 1
        """
    )
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDICE_VIEJO} ON {_TABLA} (tenant_id, evento_id_cliente)")
    op.execute(f"DROP INDEX IF EXISTS {_INDICE_NUEVO}")
    op.execute(f"ALTER TABLE {_TABLA} DROP COLUMN IF EXISTS actor")
