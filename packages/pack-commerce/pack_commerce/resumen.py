"""KPIs de Ventas para el Resumen por agente (ADR-0171): pedidos, ventas pagadas, por despachar y clientes nuevos.

Es el módulo completo del negocio, no solo lo que creó el agente. Enlaces semánticos: cada cliente
los traduce a su pantalla.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from core.admin.resumen_registry import (
    Destino,
    Kpi,
    PeriodoResumen,
    ResumenProvider,
    resumen_registry,
)
from core.db.models import Customer
from core.db.session import get_session
from pack_commerce.models import Order

PLUGIN_ID = "pack_commerce"
_FUERA = ("cancelado", "expirado")


async def kpis(tenant_id: uuid.UUID, p: PeriodoResumen) -> list[Kpi]:
    o = Order

    async def pedidos(desde, hasta) -> float:  # type: ignore[no-untyped-def]
        return float(
            (
                await session.execute(
                    select(func.count(o.id)).where(
                        o.tenant_id == tenant_id,
                        o.created_at >= desde,
                        o.created_at < hasta,
                        o.status.not_in(_FUERA),
                    )
                )
            ).scalar()
            or 0
        )

    async def ventas(desde, hasta) -> float:  # type: ignore[no-untyped-def]
        return float(
            (
                await session.execute(
                    select(func.coalesce(func.sum(o.total), 0)).where(
                        o.tenant_id == tenant_id,
                        o.created_at >= desde,
                        o.created_at < hasta,
                        o.payment_status == "pagado",
                        o.status.not_in(_FUERA),
                    )
                )
            ).scalar()
            or 0
        )

    async def clientes_nuevos(desde, hasta) -> float:  # type: ignore[no-untyped-def]
        return float(
            (
                await session.execute(
                    select(func.count(Customer.id)).where(
                        Customer.tenant_id == tenant_id,
                        Customer.deleted_at.is_(None),
                        Customer.created_at >= desde,
                        Customer.created_at < hasta,
                    )
                )
            ).scalar()
            or 0
        )

    async with get_session() as session:
        por_despachar = (
            await session.execute(
                select(func.count(o.id)).where(o.tenant_id == tenant_id, o.status == "abierto")
            )
        ).scalar() or 0
        return [
            Kpi(
                "pedidos",
                "Pedidos",
                await pedidos(p.desde, p.hasta),
                previo=await pedidos(p.previo_desde, p.previo_hasta),
                hint="Últimos 7 días",
                destino=Destino("comercio.pedidos"),
            ),
            Kpi(
                "ventas_pagadas",
                "Ventas pagadas",
                await ventas(p.desde, p.hasta),
                unidad="money",
                previo=await ventas(p.previo_desde, p.previo_hasta),
                hint="Pedidos con pago confirmado",
                destino=Destino("comercio.pedidos", {"pago": "pagado"}),
            ),
            Kpi(
                "por_despachar",
                "Pedidos por despachar",
                float(por_despachar),
                polaridad="up-bad",
                hint="Abiertos ahora",
                destino=Destino("comercio.pedidos", {"estado": "abierto"}),
            ),
            Kpi(
                "clientes_nuevos",
                "Clientes nuevos",
                await clientes_nuevos(p.desde, p.hasta),
                previo=await clientes_nuevos(p.previo_desde, p.previo_hasta),
                destino=Destino("leads"),
            ),
        ]


resumen_registry.register(
    ResumenProvider(
        plugin_id=PLUGIN_ID, agent_key="ventas", nombre="Ventas", seccion=PLUGIN_ID, kpis=kpis
    )
)
