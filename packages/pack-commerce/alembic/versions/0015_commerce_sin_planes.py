"""Commerce vende solo productos: se retiran las columnas de plan (SPEC-43, HU-COM-17, ADR-0177).

Los planes y la inscripción viven en `pack-subscriptions`. Esta migración limpia lo que commerce
guardaba para ellos.

Datos (en este orden, antes de quitar las columnas, porque se apoyan en ellas):

1. Las variantes con `billing_period` (los planes) quedan **inactivas** (`active = false`). La
   variante no tiene `deleted_at` (solo el producto lo tiene desde `crm_0014`), así que desactivar es
   su retiro lógico (invariante 8), el mismo que usa el retiro de datos de prueba.
2. Los productos cuyas variantes son TODAS planes quedan con `deleted_at`. Un producto con variantes
   mixtas (plan y no plan) se conserva: solo pierde sus variantes-plan.
3. Las promociones (`pack_commerce_promotions`) que apuntaban a un producto-plan (el del punto 2) o a
   una variante-plan quedan `active = false`; las demás no se tocan (las promociones de producto siguen igual).

Esquema: se eliminan `billing_period`, `trial_days` y `entitlements` de las variantes y
`plan_activation_status` (con su CHECK), `plan_activation_error` y `datos_alta` de los pedidos.

**Esta migración borra datos de plan de commerce y no se revierte en datos.** `downgrade` recrea las
columnas (todas nullable y vacías) y el CHECK, pero no vuelve a activar ni a restaurar los productos y
promociones que `upgrade` retiró, ni los valores de las columnas: los planes nacen ahora en
`pack_subscriptions` (decisión del dueño, D-55: los datos de test son descartables).

Las tablas ya tienen `tenant_id` y RLS forzada (`crm_0005`); aquí no se crean tablas. FORCE RLS alcanza
al dueño de la tabla, así que las actualizaciones masivas (sin `app.tenant_id`) apagan RLS un momento
y la restauran de inmediato (mismo patrón de `pack_subscriptions_0010`).

Revision ID: crm_0015
Revises: crm_0014
Create Date: 2026-10-30
"""

from __future__ import annotations

from alembic import op

revision = "crm_0015"
down_revision = "crm_0014"
branch_labels = None
depends_on = None

VARIANTS = "pack_commerce_product_variants"
PRODUCTS = "pack_commerce_products"
PROMOS = "pack_commerce_promotions"
ORDERS = "pack_commerce_orders"
TABLAS = (VARIANTS, PRODUCTS, PROMOS)


def _con_rls_apagada() -> None:
    for tabla in TABLAS:
        op.execute(f"alter table {tabla} disable row level security")


def _restaurar_rls() -> None:
    for tabla in TABLAS:
        op.execute(f"alter table {tabla} enable row level security")
        op.execute(f"alter table {tabla} force row level security")


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    _con_rls_apagada()
    # 1. Productos cuyas variantes son todas planes (con al menos una): se calculan ANTES de desactivar.
    op.execute(
        f"""
        create temporary table _crm_0015_productos_plan on commit drop as
        select p.id, p.tenant_id from {PRODUCTS} p
        where p.deleted_at is null
          and exists (select 1 from {VARIANTS} v where v.tenant_id = p.tenant_id and v.product_id = p.id
                      and v.billing_period is not null)
          and not exists (select 1 from {VARIANTS} v where v.tenant_id = p.tenant_id and v.product_id = p.id
                          and v.billing_period is null)
        """
    )
    # 2. Promociones que apuntaban a un producto-plan o a una variante-plan.
    op.execute(
        f"""
        update {PROMOS} pr set active = false, updated_at = now()
        where pr.active
          and (
            pr.variant_id in (select v.id from {VARIANTS} v where v.tenant_id = pr.tenant_id and v.billing_period is not null)
            or pr.product_id in (select t.id from _crm_0015_productos_plan t where t.tenant_id = pr.tenant_id)
          )
        """
    )
    # 3. Variantes-plan inactivas y productos-plan con borrado lógico.
    op.execute(f"update {VARIANTS} set active = false where billing_period is not null and active")
    op.execute(
        f"update {PRODUCTS} p set deleted_at = now(), updated_at = now() "
        f"where (p.tenant_id, p.id) in (select tenant_id, id from _crm_0015_productos_plan)"
    )
    _restaurar_rls()

    # Esquema.
    op.execute(f"alter table {VARIANTS} drop column if exists billing_period")
    op.execute(f"alter table {VARIANTS} drop column if exists trial_days")
    op.execute(f"alter table {VARIANTS} drop column if exists entitlements")
    op.execute(f"alter table {ORDERS} drop constraint if exists ck_pack_commerce_orders_plan_activation_status")
    op.execute(f"alter table {ORDERS} drop column if exists plan_activation_status")
    op.execute(f"alter table {ORDERS} drop column if exists plan_activation_error")
    op.execute(f"alter table {ORDERS} drop column if exists datos_alta")


def downgrade() -> None:
    """Recrea el esquema (columnas nullable y vacías); los datos retirados por `upgrade` no vuelven."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"alter table {ORDERS} add column if not exists datos_alta jsonb")
    op.execute(f"alter table {ORDERS} add column if not exists plan_activation_error text")
    op.execute(f"alter table {ORDERS} add column if not exists plan_activation_status text")
    op.execute(f"alter table {ORDERS} drop constraint if exists ck_pack_commerce_orders_plan_activation_status")
    op.create_check_constraint(
        "ck_pack_commerce_orders_plan_activation_status",
        ORDERS,
        "plan_activation_status is null or plan_activation_status in ('pendiente', 'activada')",
    )
    op.execute(f"alter table {VARIANTS} add column if not exists entitlements jsonb")
    op.execute(f"alter table {VARIANTS} add column if not exists trial_days integer")
    op.execute(f"alter table {VARIANTS} add column if not exists billing_period text")
