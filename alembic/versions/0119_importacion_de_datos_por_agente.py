"""Importar datos existentes por agente (HU-QA-12, ADR-0180).

`test_data_records.origen` admite `'importacion'`: el lote de lo que el dueño importó desde un CSV.
«Generar datos de prueba» y su borrado no lo tocan; «Borrar lo importado» retira solo ese origen.
No hay tablas nuevas ni cambios de RLS: la tabla ya está forzada por tenant (0098).

Se aplica al arrancar la API. La tabla es pequeña (un registro por fila sembrada), así que recrear el
CHECK es instantáneo. Respaldo previo recomendado, como toda migración.

**Rollback:** el código anterior, después el downgrade. El downgrade borra las filas de seguimiento de
origen `importacion` (los datos importados quedan en sus tablas, sin lote que los retire) y restaura el CHECK.

Revision ID: 0119
Revises: 0118
Create Date: 2026-10-28
"""

from __future__ import annotations

from alembic import op

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE test_data_records DROP CONSTRAINT IF EXISTS ck_test_data_records_origen")
    op.execute(
        "ALTER TABLE test_data_records ADD CONSTRAINT ck_test_data_records_origen "
        "CHECK (origen IN ('llm', 'plantilla', 'importacion'))"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DELETE FROM test_data_records WHERE origen = 'importacion'")
    op.execute("ALTER TABLE test_data_records DROP CONSTRAINT IF EXISTS ck_test_data_records_origen")
    op.execute(
        "ALTER TABLE test_data_records ADD CONSTRAINT ck_test_data_records_origen "
        "CHECK (origen IN ('llm', 'plantilla'))"
    )
