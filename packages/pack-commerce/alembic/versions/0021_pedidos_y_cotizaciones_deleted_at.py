"""Borrado lógico de pedidos y cotizaciones (HU-AOS-71, ❓7).

Un negocio que apaga comercio (`solvershub`, que solo vende suscripciones) oculta sus productos,
pedidos y cotizaciones sin borrarlos (invariante 8): quedan en la base, no se ven en el panel, la
APK, el agente ni los crons, y no reaparecen si comercio se vuelve a encender. Los productos ya
tenían `deleted_at` (`crm_0014`); aquí se agrega a `pack_commerce_orders` y `pack_commerce_quotes`.

Índices únicos de pedidos: `uq_crm_orders_tenant_idem`, `uq_crm_orders_tenant_seq` y
`uq_crm_orders_tenant_number` siguen **completos** a propósito. Un pedido oculto nunca se recrea, y
volverlos parciales dejaría que un reintento con la misma `idempotency_key` cree un pedido nuevo
sobre uno oculto, o que dos pedidos compartan número público (la referencia del pago). Las
cotizaciones no tienen unicidad. Solo se agrega el índice parcial de vivos, como en `crm_0014`.

`ADD COLUMN` sin default es solo metadato (sin reescritura) y las tablas son chicas: los índices
se crean sin `CONCURRENTLY`, dentro de la transacción de Alembic, con `lock_timeout`.

Revision ID: crm_0021
Revises: crm_0020
Create Date: 2026-11-21
"""

from __future__ import annotations

from alembic import op

revision = "crm_0021"
down_revision = "crm_0020"
branch_labels = None
depends_on = None

_TABLAS = ("pack_commerce_orders", "pack_commerce_quotes")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for tabla in _TABLAS:
        op.execute(f"ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS deleted_at timestamptz")
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_{tabla}_tenant_vivos ON {tabla} (tenant_id) WHERE deleted_at IS NULL")


def downgrade() -> None:
    # Pierde qué pedidos y cotizaciones estaban ocultos: volverían a verse. Solo antes de usar el comando.
    op.execute("SET LOCAL lock_timeout = '5s'")
    for tabla in _TABLAS:
        op.execute(f"DROP INDEX IF EXISTS ix_{tabla}_tenant_vivos")
        op.execute(f"ALTER TABLE {tabla} DROP COLUMN IF EXISTS deleted_at")
