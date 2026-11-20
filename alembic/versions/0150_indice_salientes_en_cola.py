"""Índice parcial de los outbound `queued` de `messages` (H3, salientes atascadas).

El barrido de salientes atascadas (`core/tasks/outbound_sweeper.py`, cada 5 min) busca, negocio por
negocio, el primer outbound `queued` de cada conversación. Sin este índice, la consulta recorre
todos los mensajes del negocio con `ix_messages_tenant_conv_created`, que no filtra por estado.
Las mismas columnas sirven a `_is_earliest_queued` y `_enqueue_next` de la cola de salida.

Parcial: solo entran las filas `queued` de salida, que son pocas y de vida corta; no agrega costo
a la escritura de los mensajes ya enviados ni de los de entrada. Empieza por `tenant_id`.

`CREATE INDEX CONCURRENTLY` fuera de la transacción (no bloquea escrituras en `messages`).
Idempotente: un índice válido se salta y uno INVALID de un intento interrumpido se rehace.
Sin cambios de RLS: `messages` ya tiene `tenant_id`, RLS forzada y policy.

Reversible: el downgrade borra el índice.

Revision ID: 0150
Revises: 0149
Create Date: 2026-11-20
"""

from __future__ import annotations

from alembic import op

revision = "0150"
down_revision = "0149"
branch_labels = None
depends_on = None

_NOMBRE = "ix_messages_outbound_queued"
_DEFINICION = (
    "messages (tenant_id, conversation_id, created_at, id) "
    "WHERE direction = 'outbound' AND status = 'queued'"
)


def _estado() -> bool | None:
    """None si no existe; False si quedó INVALID; True si existe válido."""
    fila = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %(nombre)s",
            {"nombre": _NOMBRE},
        )
        .first()
    )
    return None if fila is None else bool(fila[0])


def upgrade() -> None:
    with op.get_context().autocommit_block():
        estado = _estado()
        if estado:
            return
        if estado is False:  # quedó INVALID de un CONCURRENTLY interrumpido
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NOMBRE}")
        op.execute(f"CREATE INDEX CONCURRENTLY {_NOMBRE} ON {_DEFINICION}")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NOMBRE}")
