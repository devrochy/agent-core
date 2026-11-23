"""`subscription_payments.provider_transaction_id`: una transacción aprobada del proveedor se aplica una sola vez.

Hallazgo de la revisión de seguridad de SPEC-51: la firma del webhook de Wompi cubre `transaction.id`,
`transaction.status` y `transaction.amount_in_cents`, pero no `transaction.payment_link_id` (Wompi elige las
propiedades firmadas y no admite pedir otras). Un evento aprobado y firmado de otra transacción del mismo monto,
reenviado con otro `payment_link_id`, podía acreditar un plan o una recarga ajenos. Desde aquí `apply_payment`
guarda el `transaction.id` que aprobó cada pago y un índice único lo frena si llega de nuevo para otro pago.

- `provider_transaction_id text null`: null en los pagos viejos y en los que todavía no se aprobaron.
- Índice único parcial `uq_subscription_payments_transaccion (provider, provider_transaction_id)` WHERE
  `provider_transaction_id IS NOT NULL`. **No empieza por `tenant_id` a propósito:** el reenvío del ataque es
  entre negocios, y dentro del `tenant_scope` la RLS no deja ver la fila ajena. Solo un índice global lo frena
  (la unicidad se comprueba sin RLS). Es un índice adicional: la tabla sigue con su índice por `tenant_id`, RLS
  y permisos sin cambios.

Aditiva: una columna nullable sin default (no reescribe) y un índice sobre una tabla chica (unas filas por negocio
y por mes), con `lock_timeout` de 5 s. El downgrade borra el índice y la columna.

Revision ID: 0158
Revises: 0157
Create Date: 2026-11-22
"""

from __future__ import annotations

from alembic import op

revision = "0158"
down_revision = "0157"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE subscription_payments ADD COLUMN provider_transaction_id text")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_subscription_payments_transaccion
          ON subscription_payments (provider, provider_transaction_id)
          WHERE provider_transaction_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS uq_subscription_payments_transaccion")
    op.execute("ALTER TABLE subscription_payments DROP COLUMN IF EXISTS provider_transaction_id")
