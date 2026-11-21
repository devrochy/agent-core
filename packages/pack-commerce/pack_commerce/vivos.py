"""Pedidos y cotizaciones vivos (HU-AOS-71): los ocultos (`deleted_at`, `crm_0021`) no se leen.

`get_owned` del core no mira `deleted_at`; las lecturas por id de pedidos y cotizaciones pasan por aquí.
Las listas y agregados filtran en su propia consulta con `Order.deleted_at.is_(None)`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from pack_commerce.models import Order, Quote


async def pedido_vivo(session: Any, order_id: uuid.UUID, tenant_id: uuid.UUID) -> Order | None:
    return (
        await session.execute(
            select(Order).where(Order.tenant_id == tenant_id, Order.id == order_id, Order.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


async def cotizacion_viva(session: Any, quote_id: uuid.UUID, tenant_id: uuid.UUID) -> Quote | None:
    return (
        await session.execute(
            select(Quote).where(Quote.tenant_id == tenant_id, Quote.id == quote_id, Quote.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
