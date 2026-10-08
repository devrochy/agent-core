"""Planes como producto (ADR-0083) y regla D-16 (HU-SUB-06).

- Solo un plan de **$0** (Free) tiene prueba: un `trial_days` en una variante paga se
  ignora y queda registrado para el panel.
- Un pedido cuyo único contenido son planes en $0 no se cobra: nace "pagado" y la
  suscripción se activa en el acto (en `trialing`).
- Un plan por contra entrega NO nace pagado: espera el pago real.

Commerce lee SU propia variante y entrega el snapshot al proveedor de suscripciones vía
el registry del core (sin importar el pack, invariante nº5).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.subscriptions import PlanSnapshot
from pack_commerce.models import Order, OrderItem, Product, ProductVariant

log = get_logger(__name__)

METODOS_DIFERIDOS = ("transferencia", "pago_en_linea")


def estado_pago_inicial(payment_method: str, es_plan: bool, total: Decimal | float) -> str:
    """Estado de pago con el que nace un pedido."""
    if es_plan and Decimal(str(total)) == 0:
        return "pagado"  # Free: no hay nada que cobrar (D-16)
    if payment_method in METODOS_DIFERIDOS:
        return "pendiente"
    # Contra entrega: un producto se da por pagado al entregar; un plan espera el pago
    # real (antes heredaba "pagado" y la suscripción quedaba activa sin cobrar).
    return "pendiente" if es_plan else "pagado"


def snapshot_de_plan(
    *, sku: str, nombre: str, precio: Decimal | float, periodicidad: str, trial_days: int | None
) -> tuple[PlanSnapshot, bool]:
    """Snapshot del plan para el proveedor de suscripciones y si se ignoró una prueba.

    D-16: solo Free ($0) tiene prueba; en un plan pago la prueba se ignora.
    """
    precio_f = float(precio or 0)
    gratis = precio_f == 0
    trial = int(trial_days or 0)
    plan: PlanSnapshot = {
        "plan_code": sku,
        "nombre": nombre,
        "precio": precio_f,
        "moneda": "COP",
        "periodicidad": periodicidad,
        "trial_dias": trial if gratis else 0,
    }
    return plan, (not gratis and trial > 0)


async def activar_planes_del_pedido(session, order: Order) -> int:
    """Activa la suscripción del cliente por cada línea del pedido con recurrencia.
    Idempotente en el proveedor. Devuelve cuántas activó."""
    items = (
        await session.execute(
            select(OrderItem).where(
                OrderItem.tenant_id == get_current_tenant_id(),
                OrderItem.order_id == order.id,
            )
        )
    ).scalars().all()
    if not items:
        return 0

    from core.agents.spec import enabled_packs, load_active_spec
    from core.subscriptions import SubscriptionError, subscription_registry

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return 0
    spec = await load_active_spec(tenant_id)
    if "pack_subscriptions" not in enabled_packs(spec):
        return 0
    capability = await subscription_registry.capability_for_tenant(tenant_id)
    if capability is None:
        log.warning("subscription_provider_missing", tenant_id=str(tenant_id))
        return 0

    activadas = 0
    for item in items:
        variant = (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.tenant_id == get_current_tenant_id(),
                    ProductVariant.id == item.variant_id,
                )
            )
        ).scalars().first()
        if variant is None or not variant.billing_period:
            continue
        product = await session.get(Product, variant.product_id)
        plan, trial_ignorado = snapshot_de_plan(
            sku=variant.sku,
            nombre=product.name if product else variant.sku,
            precio=variant.price,
            periodicidad=variant.billing_period,
            trial_days=variant.trial_days,
        )
        if trial_ignorado:
            await _avisar_trial_ignorado(variant, order)
        try:
            await capability.activate(
                customer_id=order.customer_id,
                variant_id=str(item.variant_id),
                plan=plan,
                reference=order.public_order_number,
            )
        except SubscriptionError:
            log.exception("order_activate_subscription_failed", order_id=str(order.id))
            continue
        activadas += 1
        log.info("order_activates_subscription", order_id=str(order.id), plan=plan.get("nombre"))
    return activadas


async def _avisar_trial_ignorado(variant: ProductVariant, order: Order) -> None:
    from core.shared.audit import record_audit

    log.warning("trial_ignorado_en_plan_pago", sku=variant.sku, trial_days=variant.trial_days)
    await record_audit(
        actor="plugin:pack_commerce",
        action="trial_ignorado_en_plan_pago",
        output_data={
            "sku": variant.sku,
            "trial_days": variant.trial_days,
            "order": order.public_order_number,
            "regla": "Solo el plan Free ($0) tiene prueba (D-16).",
        },
    )
