"""`subscription_payments.concepto` y `creditos`: las recargas de créditos son un pago más de la plataforma
(ADR-0209 §4, HU-BILL-23).

- `concepto text not null default 'plan'`, CHECK `in ('plan', 'recarga')`. Todas las filas existentes son
  pagos del plan: el default las cubre sin backfill (en Postgres ≥ 11 una columna con default constante no
  reescribe la tabla).
- `creditos numeric(12,1) null`, con el mismo tipo que `credit_grants.creditos`. El CHECK lo ata al
  concepto: en una recarga, `> 0` (y con tope, que en Postgres `NaN > 0` es verdadero); en un pago del plan,
  null.
- Una recarga lleva `periodo = null` y `plan_id` = el plan vigente al comprar (deja auditable el descuento).
  La conciliación, la renovación y `billing_lifecycle` ya la ignoran porque filtran `periodo is not null`;
  `apply_payment` bifurca por `concepto` antes de la rama de los pagos antiguos sin período.

Aditiva y sin locks largos: dos `ADD COLUMN` (el default constante no reescribe) y dos CHECK que se
validan sobre una tabla chica (unas filas por negocio y por mes), con `lock_timeout` de 5 s. Sin índice
nuevo: las consultas siguen entrando por `(tenant_id, periodo)` y `(tenant_id, provider_reference)`. RLS y permisos de la tabla no cambian. El downgrade borra las columnas
(solo para revertir un despliegue recién hecho, antes de vender recargas: las filas de recarga quedarían
como pagos del plan sin período).

**Numeración:** 0156 con `down_revision = "0154"` (la `credit_grants` de HU-BILL-22 en esta rama). Se
renumera al integrar; la cadena final es `0153` tts → `0154` nivel_ia → `0155` credit_grants → `0156` esta
(ADR-0209 §6).

Revision ID: 0156
Revises: 0154
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op

revision = "0156"
down_revision = "0154"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE subscription_payments ADD COLUMN concepto text NOT NULL DEFAULT 'plan'")
    op.execute("ALTER TABLE subscription_payments ADD COLUMN creditos numeric(12, 1)")
    op.execute(
        "ALTER TABLE subscription_payments ADD CONSTRAINT ck_subscription_payments_concepto "
        "CHECK (concepto IN ('plan', 'recarga'))"
    )
    op.execute(
        "ALTER TABLE subscription_payments ADD CONSTRAINT ck_subscription_payments_creditos CHECK ("
        "(concepto = 'recarga' AND creditos > 0 AND creditos < 100000000000) "
        "OR (concepto = 'plan' AND creditos IS NULL)"
        ")"
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE subscription_payments DROP CONSTRAINT IF EXISTS ck_subscription_payments_creditos")
    op.execute("ALTER TABLE subscription_payments DROP CONSTRAINT IF EXISTS ck_subscription_payments_concepto")
    op.execute("ALTER TABLE subscription_payments DROP COLUMN IF EXISTS creditos")
    op.execute("ALTER TABLE subscription_payments DROP COLUMN IF EXISTS concepto")
