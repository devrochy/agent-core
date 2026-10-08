"""Precio de un plan de la plataforma como única fuente (HU-SH-01).

Una variante que vende un plan de SolversHub declara `attributes.precio_de_plan`
(`"basic"`, `"premium"`…): su precio sale de `core/billing/plans.py`, no del
`catalog.json` ni de la voz de marca. El seed lo aplica al sembrar y el cron
`sincronizar_precios_de_plan` lo mantiene al día tras un deploy con precios nuevos.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from core.shared.logger import get_logger
from pack_commerce.models import ProductVariant

log = get_logger(__name__)

ATRIBUTO = "precio_de_plan"


def precio_de_plan(attributes: dict[str, Any] | None) -> Decimal | None:
    """Precio del plan de la plataforma que referencia la variante, o None."""
    from core.billing.plans import get_plan

    plan_id = str((attributes or {}).get(ATRIBUTO) or "").strip()
    if not plan_id:
        return None
    plan = get_plan(plan_id)
    if plan is None:
        log.warning("precio_de_plan_desconocido", plan=plan_id)
        return None
    return Decimal(plan.price_cop)


async def sincronizar(session, tenant_id: uuid.UUID) -> int:  # type: ignore[no-untyped-def]
    """Alinea el precio de las variantes con `precio_de_plan`. Devuelve cuántas cambió."""
    variantes = (
        await session.execute(
            select(ProductVariant).where(
                ProductVariant.tenant_id == tenant_id,
                ProductVariant.attributes[ATRIBUTO].astext.isnot(None),
            )
        )
    ).scalars().all()
    cambiadas = 0
    for variante in variantes:
        precio = precio_de_plan(variante.attributes)
        if precio is not None and Decimal(str(variante.price or 0)) != precio:
            log.info("precio_de_plan_actualizado", sku=variante.sku, antes=str(variante.price), ahora=str(precio))
            variante.price = precio
            cambiadas += 1
    return cambiadas
