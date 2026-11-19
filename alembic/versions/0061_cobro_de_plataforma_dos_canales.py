"""Cobro de la plataforma por dos canales con fuente única (HU-SH-04, ADR-0123).

- `subscription_payments.periodo`: referencia del período que cubre el pago
  (`plat-<slug>-<AAAAMM>`), la misma en el canal asistente y en el panel.
  - Índice único parcial `(tenant_id, periodo)` sobre los `aprobado`: un período se aplica
    una sola vez a `tenant_subscriptions`. Un segundo pago del mismo período queda
    `duplicado` (estado nuevo, sin CHECK de estados hasta hoy) y se alerta.
  - Índice `(tenant_id, periodo)` para buscar los intentos de un período.
  - Relleno: el primer período del alta automática (HU-SH-03) ya guardaba la referencia en
    `provider_reference` (`provider = 'asistente'`); se copia a `periodo`.
- `tenant_subscriptions.dia_ancla`: día del mes en que empiezan los períodos pagos
  (calendario, como `pack_subscriptions`). NULL = se toma del fin del período.
- `provisioning_requests (tenant_id, negocio_tenant_id)`: índice para ir del negocio creado
  a su suscripción en el vendedor (canal asistente y conciliación).

Columnas nullable sin default: sin reescritura. Las tablas ya son de negocio (`tenant_id` +
RLS forzada) y los índices empiezan por `tenant_id`. Tablas chicas (una suscripción por
negocio, un pago por período): los índices se crean dentro de la transacción. El CHECK de
`dia_ancla` se agrega `NOT VALID` y se valida aparte (no bloquea escrituras mientras valida).

Revision ID: 0061
Revises: 0059
Create Date: 2026-10-09
"""
from __future__ import annotations

import logging

import sqlalchemy as sa

from alembic import op

revision = "0061"
down_revision = "0059"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

# Pagos del primer período del alta automática (HU-SH-03): la referencia del período ya
# estaba en `provider_reference`. Con RLS forzada solo los ve un rol que la salte (las
# migraciones corren con el dueño de la base, ver 0052); con otro rol, 0 filas y nada roto.
_RELLENO = """
    update subscription_payments
    set periodo = provider_reference
    where periodo is null
      and provider = 'asistente'
      and status = 'aprobado'
      and provider_reference ~ '^plat-.+-[0-9]{6}$'
      and deleted_at is null
"""


def upgrade() -> None:
    op.add_column("subscription_payments", sa.Column("periodo", sa.Text(), nullable=True))
    op.create_index(
        "ix_subscription_payments_tenant_periodo",
        "subscription_payments",
        ["tenant_id", "periodo"],
    )
    op.create_index(
        "uq_subscription_payments_periodo_aprobado",
        "subscription_payments",
        ["tenant_id", "periodo"],
        unique=True,
        postgresql_where=sa.text("status = 'aprobado' and periodo is not null and deleted_at is null"),
    )
    rellenadas = op.get_bind().execute(sa.text(_RELLENO)).rowcount
    log.info("0061: %s pagos del alta automática con su período", rellenadas)

    op.add_column("tenant_subscriptions", sa.Column("dia_ancla", sa.SmallInteger(), nullable=True))
    op.execute(
        "alter table tenant_subscriptions add constraint ck_tenant_subscriptions_dia_ancla "
        "check (dia_ancla between 1 and 31) not valid"
    )
    op.execute("alter table tenant_subscriptions validate constraint ck_tenant_subscriptions_dia_ancla")

    op.create_index(
        "ix_provisioning_requests_tenant_negocio",
        "provisioning_requests",
        ["tenant_id", "negocio_tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_provisioning_requests_tenant_negocio", table_name="provisioning_requests")
    op.execute("alter table tenant_subscriptions drop constraint if exists ck_tenant_subscriptions_dia_ancla")
    op.drop_column("tenant_subscriptions", "dia_ancla")
    # `status` es texto libre: un `duplicado` queda como está (el código anterior no lo lee).
    op.drop_index("uq_subscription_payments_periodo_aprobado", table_name="subscription_payments")
    op.drop_index("ix_subscription_payments_tenant_periodo", table_name="subscription_payments")
    op.drop_column("subscription_payments", "periodo")
