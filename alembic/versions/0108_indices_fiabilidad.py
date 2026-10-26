"""Agent OS · índices del tablero de fiabilidad (HU-AOS-29, adenda de ADR-0117).

Todo fuera de la transacción (`autocommit_block`), sin bloquear escrituras (patrón de la 0071):

- `ix_core_decisions_release`: métricas por versión y carril. Parcial: los turnos sin release (los
  de antes de las releases) no entran.
- `ix_conversation_tasks_completed`: completitud por fecha de cierre. Cubre todos los estados
  terminales (el CHECK ata `completed_at` a ellos); `ix_conversation_tasks_closed` solo cubre
  `completed` y `cancelled`.
- `uq_turn_feedback_falso_positivo`: un falso positivo del verificador por turno y tipo. La marca es
  idempotente con `ON CONFLICT DO NOTHING`. Hoy no hay filas con esa señal.

Idempotente: un índice válido se salta y uno INVALID de un intento interrumpido se rehace.

Revision ID: 0108
Revises: 0107
Create Date: 2026-10-25
"""

from __future__ import annotations

from alembic import op

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None

# nombre → definición después de `ON`
_INDICES = {
    "ix_core_decisions_release": (
        "",
        "core_decisions (tenant_id, release_id, created_at) WHERE release_id IS NOT NULL",
    ),
    "ix_conversation_tasks_completed": (
        "",
        "conversation_tasks (tenant_id, completed_at) WHERE completed_at IS NOT NULL",
    ),
    "uq_turn_feedback_falso_positivo": (
        "UNIQUE",
        "turn_feedback (tenant_id, turn_id, signal) "
        "WHERE source = 'owner' AND signal LIKE 'verifier_false_positive:%'",
    ),
}
# índice → (tabla, único, columnas, parcial)
_ESPERADO = {
    nombre: (
        definicion.split()[0],
        unico == "UNIQUE",
        definicion[definicion.index("(") : definicion.index(")") + 1],
        " WHERE " in definicion,
    )
    for nombre, (unico, definicion) in _INDICES.items()
}


def _indice_existente(nombre: str) -> bool | None:
    """None si no existe; False si quedó INVALID; True si existe válido y es EL MISMO índice.

    Un índice con el mismo nombre pero otra tabla, columnas, unicidad o parcialidad no se salta en
    silencio: la migración falla para que alguien lo mire.
    """
    tabla, unico, columnas, parcial = _ESPERADO[nombre]
    fila = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT i.indisvalid, t.relname, i.indisunique, pg_get_indexdef(i.indexrelid), "
            "i.indpred IS NOT NULL "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %(nombre)s",
            {"nombre": nombre},
        )
        .first()
    )
    if fila is None:
        return None
    valido, tabla_real, unico_real, definicion, parcial_real = fila
    if not valido:
        return False
    if (tabla_real, unico_real, parcial_real) != (tabla, unico, parcial) or (
        f"USING btree {columnas}" not in definicion
    ):
        raise RuntimeError(f"Ya existe un índice {nombre} distinto del esperado: {definicion}")
    return True


def _indice_concurrente(nombre: str, unico: str, definicion: str) -> None:
    estado = _indice_existente(nombre)
    if estado:
        return
    if estado is False:  # quedó INVALID de un CONCURRENTLY interrumpido
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {nombre}")
    op.execute(f"CREATE {unico} INDEX CONCURRENTLY {nombre} ON {definicion}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for nombre, (unico, definicion) in _INDICES.items():
            _indice_concurrente(nombre, unico, definicion)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for nombre in reversed(list(_INDICES)):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {nombre}")
