"""`subscription_payments.concepto` y `creditos`: las recargas de créditos son un pago más de la plataforma
(ADR-0209 §4, HU-BILL-23).

- `concepto text not null default 'plan'`, CHECK `in ('plan', 'recarga')`. Todas las filas existentes son
  pagos del plan: el default las cubre sin backfill (en Postgres ≥ 11 una columna con default constante no
  reescribe la tabla).
- `creditos numeric(12,1) null`, con el mismo tipo que `credit_grants.creditos`. El CHECK lo ata al
  concepto: en una recarga, no nulo y `> 0` (y con tope, que en Postgres `NaN > 0` es verdadero); en un pago
  del plan, null. Otro CHECK exige `periodo` null en una recarga, así nunca entra al índice único del período.
- Una recarga lleva `periodo = null` y `plan_id` = el plan vigente al comprar (deja auditable el descuento).
  La conciliación, la renovación y `billing_lifecycle` ya la ignoran porque filtran `periodo is not null`;
  `apply_payment` bifurca por `concepto` antes de la rama de los pagos antiguos sin período.

Aditiva y sin locks largos: un solo `ALTER TABLE` con dos `ADD COLUMN` (el default constante no reescribe)
y tres CHECK que se validan sobre una tabla chica (unas filas por negocio y por mes), con `lock_timeout` de 5 s. Sin índice
nuevo: las consultas siguen entrando por `(tenant_id, periodo)` y `(tenant_id, provider_reference)`. RLS y permisos de la tabla no cambian. El downgrade borra las columnas
(solo para revertir un despliegue recién hecho, antes de vender recargas: las filas de recarga quedarían
como pagos del plan sin período y sus `credit_grants` sin forma de distinguirlas; no se agrega una guarda
porque el rol de migraciones no ve filas con la RLS forzada y sin negocio en contexto).

**Numeración:** 0156, después de `0155_credit_grants` (HU-BILL-22), como fija ADR-0209 §6: `0153` tts → `0154` nivel_ia → `0155` credit_grants → `0156` esta.

Revision ID: 0156
Revises: 0155
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op

revision = "0156"
down_revision = "0155"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Un solo ALTER: el ACCESS EXCLUSIVE se toma una vez. `creditos IS NOT NULL` explícito: sin él, una
    # recarga con `creditos` null daría NULL en el CHECK y Postgres lo aceptaría.
    op.execute(
        """
        ALTER TABLE subscription_payments
          ADD COLUMN concepto text NOT NULL DEFAULT 'plan',
          ADD COLUMN creditos numeric(12, 1),
          ADD CONSTRAINT ck_subscription_payments_concepto CHECK (concepto IN ('plan', 'recarga')),
          ADD CONSTRAINT ck_subscription_payments_creditos CHECK (
            (concepto = 'recarga' AND creditos IS NOT NULL AND creditos > 0 AND creditos < 100000000000)
            OR (concepto = 'plan' AND creditos IS NULL)
          ),
          ADD CONSTRAINT ck_subscription_payments_recarga_sin_periodo CHECK (
            concepto <> 'recarga' OR periodo IS NULL
          )
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "ALTER TABLE subscription_payments DROP CONSTRAINT IF EXISTS ck_subscription_payments_recarga_sin_periodo"
    )
    op.execute("ALTER TABLE subscription_payments DROP CONSTRAINT IF EXISTS ck_subscription_payments_creditos")
    op.execute("ALTER TABLE subscription_payments DROP CONSTRAINT IF EXISTS ck_subscription_payments_concepto")
    op.execute("ALTER TABLE subscription_payments DROP COLUMN IF EXISTS creditos")
    op.execute("ALTER TABLE subscription_payments DROP COLUMN IF EXISTS concepto")
