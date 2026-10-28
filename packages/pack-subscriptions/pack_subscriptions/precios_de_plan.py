"""El precio de un plan de la plataforma sale de `core/billing/plans.py` (SPEC-43, HU-SUB-32).

Un plan del catálogo con `plan_plataforma` ("basic", "premium"…) no tiene precio propio: lo fija el
catálogo de la plataforma. La siembra lo aplica al sembrar y el cron `sincronizar_precios_de_plan`
lo mantiene tras un deploy con precios nuevos. Solo cambia el plan: las suscripciones ya inscritas
conservan el precio de cuando entraron (snapshot).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from core.shared.logger import get_logger
from pack_subscriptions.models import Plan

log = get_logger(__name__)


def precio_de_plataforma(plan_plataforma: str | None) -> Decimal | None:
    """Precio del plan de la plataforma, o None si no hay referencia o ya no existe en el catálogo."""
    from core.billing.plans import get_plan

    clave = str(plan_plataforma or "").strip()
    if not clave:
        return None
    plan = get_plan(clave)
    if plan is None:
        log.warning("precio_de_plan_desconocido", plan=clave)
        return None
    return Decimal(plan.price_cop)


async def sincronizar(session, tenant_id: uuid.UUID) -> int:  # type: ignore[no-untyped-def]
    """Alinea el precio de los planes vigentes con su `plan_plataforma`. Devuelve cuántos cambió.

    Un plan con prueba (`trial_dias > 0`, solo válido a $0, D-16) que pasaría a costar más NO se toca:
    el CHECK del esquema lo rechazaría y la prueba quedaría en un plan de pago; se avisa para que el
    operador decida."""
    planes = (
        await session.execute(
            select(Plan).where(
                Plan.tenant_id == tenant_id, Plan.deleted_at.is_(None), Plan.plan_plataforma.is_not(None)
            )
        )
    ).scalars().all()
    cambiados = 0
    for plan in planes:
        precio = precio_de_plataforma(plan.plan_plataforma)
        if precio is None or Decimal(str(plan.precio or 0)) == precio:
            continue
        if precio > 0 and int(plan.trial_dias or 0) > 0:
            log.error("precio_de_plan_con_prueba", codigo=plan.codigo, plan=plan.plan_plataforma, precio=str(precio))
            continue
        log.info("precio_de_plan_actualizado", codigo=plan.codigo, antes=str(plan.precio), ahora=str(precio))
        plan.precio = precio
        cambiados += 1
    return cambiados
