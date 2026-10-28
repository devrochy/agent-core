"""Datos de prueba por módulo (ajuste de HU-QA-10).

1. `test_data_records.agent_id` deja de ser obligatorio: el botón vive ahora también en cada
   módulo (Ventas, Agenda, Cobranza, Suscripciones) y en el onboarding, donde todavía no hay
   agente. Las filas antiguas conservan su agente.
2. `test_data_records.modulo` agrupa el lote por módulo del negocio, aunque lo siembre más de un
   pack (Suscripciones usa Comercio para los planes y Suscripciones para los clientes/cargos).
   Las filas antiguas se rellenan desde `pack_id` + `modo`.
3. Índice por `(tenant_id, modulo)` para el estado y el retiro por módulo.

**Rollback:** el código anterior, después el downgrade. Para volver `agent_id` a NOT NULL se
descartan las filas de seguimiento de lotes generados desde un módulo (solo `test_data_records`;
los datos sembrados en los packs no se tocan desde esta migración).

Revision ID: 0115
Revises: 0114
Create Date: 2026-10-28
"""

from __future__ import annotations

from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE test_data_records ALTER COLUMN agent_id DROP NOT NULL")
    op.execute("ALTER TABLE test_data_records ADD COLUMN IF NOT EXISTS modulo text")
    op.execute(
        """
        UPDATE test_data_records
        SET modulo = CASE
            WHEN pack_id = 'pack_calendar' AND modo = 'agenda' THEN 'agenda'
            WHEN pack_id = 'pack_cobranza' AND modo = 'cartera' THEN 'cobranza'
            WHEN pack_id = 'pack_commerce' AND modo = 'planes' THEN 'suscripciones'
            WHEN pack_id = 'pack_commerce' AND modo = 'catalogo' THEN 'ventas'
            ELSE NULL
        END
        WHERE modulo IS NULL
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_test_data_records_modulo "
        "ON test_data_records (tenant_id, modulo, created_at DESC) WHERE deleted_at IS NULL"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS ix_test_data_records_modulo")
    op.execute("DELETE FROM test_data_records WHERE agent_id IS NULL")
    op.execute("ALTER TABLE test_data_records ALTER COLUMN agent_id SET NOT NULL")
    op.execute("ALTER TABLE test_data_records DROP COLUMN IF EXISTS modulo")
