"""Crédito mensual en `usage_ledger` (HU-BILL-04/06, D-35, ADR-0144).

`modelo-de-precios.md` §6 (opción A, ADR-0045): los créditos viven en `usage_ledger`, sin tabla
aparte. Columnas nuevas, todas nullables y sin default (PG ≥ 11: sin reescritura ni escaneo):

- `credits numeric(12,1)`: créditos del turno, calculados AL ESCRIBIR con el tarifario y la TRM
  vigentes (1 crédito = COP 10). NULL en las filas anteriores: se calculan al leer (interino).
- `key_mode text`: `platform` | `byok` (con llave propia solo se cobra el fee).
- `tarifario_version text`: versión del tarifario (`v1`).
- `trm_cop numeric(10,2)`: COP por USD con que se convirtió el costo (setting `BILLING_TRM_COP`).

CHECKs `NOT VALID` (no escanean; aplican a las filas nuevas). Las filas viejas los cumplen (todo
NULL), así que validarlos después es seguro; se deja para el contract de HU-BILL-04.

Sin UPDATE: el trigger `usage_ledger_immutable` (0003) lo prohíbe y las filas viejas no se
reescriben. La tabla ya tiene RLS forzada e índice `(tenant_id, created_at)`; no cambia nada de eso.

**Orden del deploy:** primero la migración, después el código (el ORM inserta las columnas nuevas).
**Rollback:** primero el código anterior, después el `downgrade`, que borra las columnas y con ellas
los créditos congelados: la lectura los vuelve a calcular con la TRM actual y **como plataforma**, así que
los turnos con llave propia (BYOK) ya medidos pasan a contar costo + margen en vez de solo el fee. Si eso
importa, basta con volver el código y no correr el `downgrade` (las columnas son nullables e inocuas).

Revision ID: 0096
Revises: 0095
Create Date: 2026-10-19
"""

from __future__ import annotations

from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Toma ACCESS EXCLUSIVE un instante (solo catálogo): si hay un turno escribiendo, espera
    # como mucho 5 s y falla en vez de encolar a todos los turnos detrás.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE usage_ledger
          ADD COLUMN IF NOT EXISTS credits numeric(12,1),
          ADD COLUMN IF NOT EXISTS key_mode text,
          ADD COLUMN IF NOT EXISTS tarifario_version text,
          ADD COLUMN IF NOT EXISTS trm_cop numeric(10,2)
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_usage_credits'
                         AND conrelid = 'usage_ledger'::regclass) THEN
            ALTER TABLE usage_ledger ADD CONSTRAINT ck_usage_credits
              CHECK (credits IS NULL OR credits >= 0) NOT VALID;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_usage_key_mode'
                         AND conrelid = 'usage_ledger'::regclass) THEN
            ALTER TABLE usage_ledger ADD CONSTRAINT ck_usage_key_mode
              CHECK (key_mode IS NULL OR key_mode IN ('platform','byok')) NOT VALID;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_usage_trm'
                         AND conrelid = 'usage_ledger'::regclass) THEN
            ALTER TABLE usage_ledger ADD CONSTRAINT ck_usage_trm
              CHECK (trm_cop IS NULL OR trm_cop > 0) NOT VALID;
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_usage_credits_completos'
                         AND conrelid = 'usage_ledger'::regclass) THEN
            -- Una fila con créditos dice con qué los calculó.
            ALTER TABLE usage_ledger ADD CONSTRAINT ck_usage_credits_completos
              CHECK (credits IS NULL OR (key_mode IS NOT NULL AND tarifario_version IS NOT NULL
                                         AND trm_cop IS NOT NULL)) NOT VALID;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        ALTER TABLE usage_ledger
          DROP CONSTRAINT IF EXISTS ck_usage_credits_completos,
          DROP CONSTRAINT IF EXISTS ck_usage_trm,
          DROP CONSTRAINT IF EXISTS ck_usage_key_mode,
          DROP CONSTRAINT IF EXISTS ck_usage_credits,
          DROP COLUMN IF EXISTS trm_cop,
          DROP COLUMN IF EXISTS tarifario_version,
          DROP COLUMN IF EXISTS key_mode,
          DROP COLUMN IF EXISTS credits
        """
    )
