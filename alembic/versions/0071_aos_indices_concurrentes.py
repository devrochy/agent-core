"""Agent OS · expand 5/5: índices concurrentes en tablas grandes y `VALIDATE` de `ck_usage_kind` (HU-AOS-02).

Todo fuera de la transacción (`autocommit_block`), sin bloquear escrituras:

- `uq_cc_webchat_key`: índice único parcial GLOBAL de la clave pública del widget (`external_id`)
  para `type_id = 'webchat'` (ADR-0115 §4: el endpoint público resuelve sin negocio). El aislamiento
  lo cubre aparte `uq_connection_tenant_provider_external`, que empieza por `tenant_id`. Hoy no hay
  filas `webchat`: el tipo llega con HU-AOS-11.
- `ix_messages_turn`, `ix_usage_ledger_conn`, `ix_usage_ledger_agent`, `ix_usage_ledger_turn` y
  `ix_audit_log_turn`: atribución por turno, conexión y agente (diseño §4.5). Todos empiezan por
  `tenant_id` y son parciales: las filas viejas (sin turno, conexión ni agente) no entran y no
  suman escritura al camino caliente del ledger.
- `VALIDATE CONSTRAINT ck_usage_kind`: toma SHARE UPDATE EXCLUSIVE (no frena lecturas ni
  escrituras); todas las filas tienen `kind = 'turn'` por el default.

Idempotente: un índice válido se salta y uno INVALID de un intento interrumpido se rehace.

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-09
"""

from __future__ import annotations

from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None

# nombre → definición después de `ON`
_INDICES = {
    "uq_cc_webchat_key": (
        "UNIQUE",
        "channel_connections (external_id) WHERE type_id = 'webchat' AND deleted_at IS NULL",
    ),
    "ix_messages_turn": ("", "messages (tenant_id, turn_id) WHERE turn_id IS NOT NULL"),
    "ix_usage_ledger_conn": (
        "",
        "usage_ledger (tenant_id, connection_id, created_at) WHERE connection_id IS NOT NULL",
    ),
    "ix_usage_ledger_agent": (
        "",
        "usage_ledger (tenant_id, agent_instance_id, created_at) WHERE agent_instance_id IS NOT NULL",
    ),
    "ix_usage_ledger_turn": ("", "usage_ledger (tenant_id, turn_id) WHERE turn_id IS NOT NULL"),
    "ix_audit_log_turn": ("", "audit_log (tenant_id, turn_id) WHERE turn_id IS NOT NULL"),
}


def _indice_concurrente(nombre: str, unico: str, definicion: str) -> None:
    conn = op.get_bind()
    valido = conn.exec_driver_sql(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = %(nombre)s",
        {"nombre": nombre},
    ).scalar()
    if valido:
        return
    if valido is False:  # quedó INVALID de un CONCURRENTLY interrumpido
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {nombre}")
    op.execute(f"CREATE {unico} INDEX CONCURRENTLY {nombre} ON {definicion}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for nombre, (unico, definicion) in _INDICES.items():
            _indice_concurrente(nombre, unico, definicion)
        op.execute("ALTER TABLE usage_ledger VALIDATE CONSTRAINT ck_usage_kind")


def downgrade() -> None:
    # `ck_usage_kind` validada no se "des-valida": la borra el downgrade de 0070.
    with op.get_context().autocommit_block():
        for nombre in reversed(list(_INDICES)):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {nombre}")
