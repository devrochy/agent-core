"""Planes como producto (ADR-0083) y regla D-16 (HU-SUB-06).

- Solo un plan de **$0** (Free) tiene prueba: un `trial_days` en una variante paga se
  ignora y queda registrado para el panel.
- Un pedido cuyo único contenido son planes en $0 no se cobra: nace "pagado" y la
  suscripción se activa en el acto (en `trialing`).
- Un plan por contra entrega NO nace pagado: espera el pago real.

Commerce lee SU propia variante y entrega el snapshot al proveedor de suscripciones que
el core resuelve para el negocio (HU-SUB-09): sin nombrar ningún pack (invariante nº5).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

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

    El proveedor lo resuelve el core según la configuración del negocio (HU-SUB-09):
    commerce no nombra ningún pack. Si no hay un proveedor claro, el pedido sigue
    pagado y la activación queda `pendiente` con el motivo (panel + aviso a los
    dueños, una vez); `reintentar_activaciones` la retoma. Idempotente en el
    proveedor. Devuelve cuántas activó.
    """
    from core.subscriptions import SubscriptionError, capability_del_negocio

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return 0
    planes = await _lineas_de_plan(session, order)
    if not planes:
        return 0

    try:
        capability = await capability_del_negocio(tenant_id)
    except SubscriptionError as exc:
        await _activacion_pendiente(order, exc.message)
        return 0
    if capability is None:
        await _activacion_pendiente(order, SIN_PROVEEDOR)
        return 0

    activadas = 0
    fallos: list[str] = []
    for item, variant in planes:
        product = await session.get(Product, variant.product_id)
        plan, trial_ignorado = snapshot_de_plan(
            sku=variant.sku,
            nombre=product.name if product else variant.sku,
            precio=variant.price,
            periodicidad=variant.billing_period or "mensual",
            trial_days=variant.trial_days,
        )
        if trial_ignorado and order.plan_activation_status != "pendiente":
            await _avisar_trial_ignorado(variant, order)
        try:
            await capability.activate(
                customer_id=order.customer_id,
                variant_id=str(item.variant_id),
                plan=plan,
                reference=order.public_order_number,
            )
        except SubscriptionError as exc:
            log.warning("order_activate_subscription_failed", order_id=str(order.id), error=exc.message)
            fallos.append(exc.message)
            continue
        activadas += 1
        log.info(
            "order_activates_subscription",
            order_id=str(order.id),
            plan=plan.get("nombre"),
            provider=capability.provider_id,
        )
    if fallos:
        await _activacion_pendiente(order, "; ".join(fallos))
    else:
        order.plan_activation_status = "activada"
        order.plan_activation_error = None
    return activadas


SIN_PROVEEDOR = (
    "No hay una habilidad de suscripciones activa: activa Suscripciones (cobro en línea) "
    "o Cobranza (cobro en campo) para vender planes recurrentes."
)


async def _lineas_de_plan(session, order: Order) -> list[tuple[OrderItem, ProductVariant]]:
    items = (
        await session.execute(
            select(OrderItem).where(
                OrderItem.tenant_id == get_current_tenant_id(),
                OrderItem.order_id == order.id,
            )
        )
    ).scalars().all()
    lineas: list[tuple[OrderItem, ProductVariant]] = []
    for item in items:
        variant = (
            await session.execute(
                select(ProductVariant).where(
                    ProductVariant.tenant_id == get_current_tenant_id(),
                    ProductVariant.id == item.variant_id,
                )
            )
        ).scalars().first()
        if variant is not None and variant.billing_period:
            lineas.append((item, variant))
    return lineas


async def _activacion_pendiente(order: Order, motivo: str) -> None:
    """Deja la activación pendiente; avisa a los dueños solo la primera vez."""
    primera = order.plan_activation_status != "pendiente"
    order.plan_activation_status = "pendiente"
    order.plan_activation_error = motivo[:500]
    log.warning("order_plan_activation_pendiente", order_id=str(order.id), motivo=motivo)
    if not primera:
        return
    from core.shared.audit import record_audit

    await record_audit(
        actor="plugin:pack_commerce",
        action="activacion_plan_pendiente",
        output_data={"order": order.public_order_number, "motivo": motivo},
    )
    try:
        from core.tasks.push_tasks import notify_owners_push

        notify_owners_push.delay(
            str(get_current_tenant_id()),
            "Plan pagado sin activar",
            f"El pedido {order.public_order_number} está pagado pero su plan no se activó: {motivo}",
            {"kind": "activacion_plan_pendiente", "order_id": str(order.id)},
        )
    except Exception:
        log.warning("activacion_plan_push_fallido", order_id=str(order.id), exc_info=True)


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


# ------------------------------------------------- catálogo de planes (HU-SUB-07, ADR-0111)
class CatalogoDePlanes:
    """Plan por SKU o `variant_id` desde el catálogo de commerce, para el core
    (`plan_catalog_registry`): el precio de un cambio de plan sale de acá, no del LLM."""

    provider_id = "pack_commerce"

    async def plan_por_codigo(self, codigo: str) -> tuple[str, PlanSnapshot] | None:
        import uuid as _uuid

        from core.db.session import get_session

        tenant_id = get_current_tenant_id()
        valor = str(codigo or "").strip()
        if tenant_id is None or not valor:
            return None
        try:
            condicion = ProductVariant.id == _uuid.UUID(valor)
        except ValueError:
            condicion = ProductVariant.sku == valor
        async with get_session() as session:
            fila = (
                await session.execute(
                    select(ProductVariant, Product)
                    .join(Product, Product.id == ProductVariant.product_id)
                    .where(
                        condicion,
                        ProductVariant.tenant_id == tenant_id,
                        ProductVariant.active.is_(True),
                        ProductVariant.billing_period.isnot(None),
                    )
                )
            ).first()
        if fila is None:
            return None
        variant, product = fila
        plan, _ = snapshot_de_plan(
            sku=variant.sku,
            nombre=product.name,
            precio=variant.price,
            periodicidad=variant.billing_period or "mensual",
            trial_days=variant.trial_days,
        )
        return str(variant.id), plan


async def segundo_plan_bloqueado(customer_id, variant_ids: list) -> dict[str, Any] | None:  # type: ignore[no-untyped-def]
    """Si el negocio no permite varias suscripciones y el cliente ya tiene una paga
    vigente a OTRO plan, el pedido no se crea: se ofrece el cambio de plan (ADR-0111)."""
    from core.subscriptions import SubscriptionError, capability_del_negocio, permite_varias

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return None
    try:
        cap = await capability_del_negocio(tenant_id)
    except SubscriptionError:
        return None  # sin proveedor claro: lo resuelve la activación (HU-SUB-09)
    if cap is None or await permite_varias(cap):
        return None
    pedidas = {str(v) for v in variant_ids}
    for vista in await cap.list_for_customer(customer_id):
        if vista.get("estado") not in ("activa", "pausada", "vencida"):
            continue
        if float(vista.get("valor_periodo") or 0) <= 0 or vista.get("plan_code") in pedidas:
            continue
        return {
            "error": "ya_tiene_suscripcion",
            "suscripcion_actual": vista.get("plan"),
            "hint": (
                "El cliente ya tiene una suscripción paga. No crees otro pedido: ofrecé "
                "cambiar de plan con sub_cambiar_plan (SKU del plan nuevo)."
            ),
        }
    return None
