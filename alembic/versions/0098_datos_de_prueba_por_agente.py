"""Datos de prueba por agente (HU-QA-10, ADR-0146).

1. `test_data_records`: una fila por registro que sembró «Generar datos de prueba» de un agente
   (categoría, variante, tipo de cita, cliente de cartera…), para reemplazarlo al regenerar y
   retirarlo con «Borrar datos de prueba» sin tocar lo que el negocio creó a mano. Los registros
   viven en tablas de packs: `pack_id` + `kind` + `record_ref` es una referencia lógica, sin FK
   (como `agent_record_origins`, 0095). `agent_id` también es lógica: el lote no debe impedir
   borrar el agente.
   - RLS por `app.tenant_id` (FORCE) y `app_runtime` con SELECT, INSERT y UPDATE: retirar un lote
     marca `deleted_at` (invariante 8). Sin DELETE.
   - Único parcial `(tenant_id, pack_id, kind, record_ref)` entre las vivas.
   - Índice `(tenant_id, agent_id)` entre las vivas: el estado y el retiro leen por agente.
2. `usage_ledger.kind` admite `'generacion'`: la llamada al LLM que genera los datos se mide como
   cualquier otra (créditos, tarifario y TRM). El CHECK se recrea `NOT VALID` (no escanea la
   tabla); se valida en el mismo paso con SHARE UPDATE EXCLUSIVE, que no frena escrituras.

Solo existe para usarla en test (`QA_TESTERS_ENABLED`), pero la migración corre en todos los
entornos: la tabla queda vacía en producción.

**Rollback:** primero el código anterior, después el `downgrade`. El downgrade falla si ya hay
filas con `kind = 'generacion'` en el ledger (es append-only): en ese caso basta con volver el código.

Revision ID: 0098
Revises: 0097
Create Date: 2026-10-20
"""

from __future__ import annotations

from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None

_RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"

_TABLA = """
CREATE TABLE IF NOT EXISTS test_data_records (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  agent_id uuid NOT NULL,
  lote_id uuid NOT NULL,
  pack_id text NOT NULL,
  modo text NOT NULL,
  kind text NOT NULL,
  record_ref uuid NOT NULL,
  origen text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  CONSTRAINT ck_test_data_records_origen CHECK (origen IN ('llm', 'plantilla'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_test_data_records_ref
  ON test_data_records (tenant_id, pack_id, kind, record_ref) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_test_data_records_agent
  ON test_data_records (tenant_id, agent_id, created_at DESC) WHERE deleted_at IS NULL;
"""

_KINDS = "'turn','agent_run','classifier','composer','embedding','eval'"


def _rls() -> None:
    op.execute("ALTER TABLE test_data_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE test_data_records FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS test_data_records_tenant_isolation ON test_data_records")
    op.execute(
        "CREATE POLICY test_data_records_tenant_isolation ON test_data_records "
        f"USING (tenant_id = {_RLS}) WITH CHECK (tenant_id = {_RLS})"
    )
    op.execute(
        """
        DO $do$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
                REVOKE ALL ON test_data_records FROM app_runtime;
                GRANT SELECT, INSERT, UPDATE ON test_data_records TO app_runtime;
            END IF;
        END $do$;
        """
    )


def _kind(kinds: str) -> None:
    op.execute("ALTER TABLE usage_ledger DROP CONSTRAINT IF EXISTS ck_usage_kind")
    op.execute(f"ALTER TABLE usage_ledger ADD CONSTRAINT ck_usage_kind CHECK (kind IN ({kinds})) NOT VALID")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(_TABLA)
    _rls()
    _kind(f"{_KINDS},'generacion'")
    op.execute("ALTER TABLE usage_ledger VALIDATE CONSTRAINT ck_usage_kind")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _kind(_KINDS)
    op.execute("ALTER TABLE usage_ledger VALIDATE CONSTRAINT ck_usage_kind")
    op.execute("DROP TABLE IF EXISTS test_data_records")
