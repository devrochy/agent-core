"""Contracción del contrato v1 de suscripciones (SPEC-43, HU-COM-17, ADR-0177).

Commerce dejó de vender planes: ya no existen `activate`, el catálogo de commerce ni el pedido que
creaba la suscripción. Esta migración retira lo que solo servía a ese camino:

- Elimina `variante_id` (la variante de commerce) y `referencia_origen` (el número del pedido), y el
  único `uq_pack_subscriptions_subs_tenant_referencia` que dependía de ellas.
- Deja `plan_id` y `numero_publico` NOT NULL: toda suscripción nace de una inscripción.

**Precheck.** Antes de apretar los NOT NULL se comprueba que ninguna fila (ni borrada lógicamente) los
tenga vacíos; si hay alguna se detiene con un mensaje que dice cuántas. La migración `0010` ya borró
las suscripciones del camino viejo, así que en un despliegue normal no hay ninguna: una fila así solo
puede venir de un entorno que no corrió `0010` en orden o de una inserción a mano, y arreglarla
(asignarle plan y número) es una decisión de una persona, no de la migración.

**Reversible en esquema, no en datos.** `downgrade` recrea las columnas nullable, quita los NOT NULL y
recrea el único; los valores de `variante_id` y `referencia_origen` no vuelven (la columna se creó
vacía).

Revision ID: pack_subscriptions_0013
Revises: pack_subscriptions_0012
Create Date: 2026-10-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "pack_subscriptions_0013"
down_revision = "pack_subscriptions_0012"
branch_labels = None
depends_on = None

SUBS = "pack_subscriptions_subscriptions"
UNICO_REFERENCIA = "uq_pack_subscriptions_subs_tenant_referencia"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # FORCE RLS alcanza también al dueño de la tabla: sin `app.tenant_id` el precheck no vería filas.
    # Se apaga RLS solo para comprobar y se restaura de inmediato (mismo patrón de 0010).
    op.execute(f"alter table {SUBS} disable row level security")
    op.execute(
        f"""
        do $$
        declare n integer;
        begin
            select count(*) into n from {SUBS} where plan_id is null or numero_publico is null;
            if n > 0 then
                raise exception
                    'pack_subscriptions_0013: % suscripciones sin plan_id o numero_publico; asígnales plan y número (o bórralas) antes de contraer el contrato', n;
            end if;
        end $$;
        """
    )
    op.execute(f"alter table {SUBS} enable row level security")
    op.execute(f"alter table {SUBS} force row level security")

    op.execute(f"drop index if exists {UNICO_REFERENCIA}")
    op.execute(f"alter table {SUBS} drop column if exists variante_id")
    op.execute(f"alter table {SUBS} drop column if exists referencia_origen")
    op.alter_column(SUBS, "plan_id", existing_type=sa.dialects.postgresql.UUID(as_uuid=True), nullable=False)
    op.alter_column(SUBS, "numero_publico", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.alter_column(SUBS, "numero_publico", existing_type=sa.Text(), nullable=True)
    op.alter_column(SUBS, "plan_id", existing_type=sa.dialects.postgresql.UUID(as_uuid=True), nullable=True)
    op.execute(f"alter table {SUBS} add column if not exists referencia_origen text")
    op.execute(f"alter table {SUBS} add column if not exists variante_id uuid")
    op.execute(
        f"create unique index if not exists {UNICO_REFERENCIA} "
        f"on {SUBS} (tenant_id, referencia_origen, variante_id) "
        "where referencia_origen is not null and deleted_at is null"
    )
