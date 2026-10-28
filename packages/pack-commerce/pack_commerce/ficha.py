"""Ficha y detalle de Ventas para el cliente (HU-CRM-14, ADR-0179 decisión 3).

`ficha` es la línea corta que entra al prompt: cuántos pedidos, el último y qué sigue abierto. NO lleva
montos, documento, dirección ni correo. `detalle` alimenta la tool `consultar_historial_cliente` y sí
trae los importes (con las claves de dinero del core, para que el verificador de montos los reconozca).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select

from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id
from pack_commerce.models import Lead, Order, Quote

_COTIZACION_ABIERTA = ("draft", "aceptada")


def _dia(valor: Any) -> str:
    return valor.strftime("%Y-%m-%d") if valor else ""


async def ficha(ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    tid = get_current_tenant_id()
    salida: dict[uuid.UUID, str] = {}
    async with get_session() as session:
        pedidos = (
            await session.execute(
                select(Order.customer_id, func.count(Order.id), func.max(Order.created_at))
                .where(Order.tenant_id == tid, Order.customer_id.in_(ids), Order.status != "expirado")
                .group_by(Order.customer_id)
            )
        ).all()
        ultimos: dict[uuid.UUID, Order] = {}
        for cid, _, _ in pedidos:
            ultimos[cid] = (
                await session.execute(
                    select(Order)
                    .where(Order.tenant_id == tid, Order.customer_id == cid, Order.status != "expirado")
                    .order_by(Order.created_at.desc())
                    .limit(1)
                )
            ).scalar_one()
        cotizaciones = dict(
            (
                await session.execute(
                    select(Quote.customer_id, func.count(Quote.id))
                    .where(Quote.tenant_id == tid, Quote.customer_id.in_(ids), Quote.status.in_(_COTIZACION_ABIERTA))
                    .group_by(Quote.customer_id)
                )
            ).all()
        )
        leads = {
            lead.customer_id: lead
            for lead in (
                await session.execute(
                    select(Lead)
                    .where(Lead.tenant_id == tid, Lead.customer_id.in_(ids))
                    .order_by(Lead.created_at.desc())
                )
            ).scalars()
        }
    cantidad = {cid: n for cid, n, _ in pedidos}
    for cid in set(cantidad) | set(cotizaciones) | set(leads):
        partes: list[str] = []
        if cid in cantidad:
            ultimo = ultimos[cid]
            partes.append(
                f"{cantidad[cid]} pedido(s); el último {ultimo.public_order_number} está {ultimo.status} "
                f"({_dia(ultimo.created_at)})"
            )
        if cotizaciones.get(cid):
            partes.append(f"{cotizaciones[cid]} cotización(es) abierta(s)")
        lead = leads.get(cid)
        if lead is not None and (lead.interest or "").strip():
            partes.append(f"interés: {lead.interest.strip()[:60]} (lead {lead.status})")
        if partes:
            salida[cid] = "; ".join(partes)
    return salida


async def detalle(customer_id: uuid.UUID, limite: int) -> list[dict[str, Any]]:
    tid = get_current_tenant_id()
    async with get_session() as session:
        pedidos = (
            await session.execute(
                select(Order)
                .where(Order.tenant_id == tid, Order.customer_id == customer_id, Order.status != "expirado")
                .order_by(Order.created_at.desc())
                .limit(limite)
            )
        ).scalars().all()
        cotizaciones = (
            await session.execute(
                select(Quote)
                .where(Quote.tenant_id == tid, Quote.customer_id == customer_id)
                .order_by(Quote.created_at.desc())
                .limit(limite)
            )
        ).scalars().all()
    filas: list[tuple[Any, dict[str, Any]]] = [
        (
            p.created_at,
            {
                "tipo": "pedido",
                "numero": p.public_order_number,
                "estado": p.status,
                "pago": p.payment_status,
                "entrega": p.delivery_method,
                "fecha": _dia(p.created_at),
                "total": float(p.total or 0),
            },
        )
        for p in pedidos
    ] + [
        (
            c.created_at,
            {"tipo": "cotizacion", "estado": c.status, "fecha": _dia(c.created_at), "total": float(c.total or 0)},
        )
        for c in cotizaciones
    ]
    filas.sort(key=lambda f: f[0], reverse=True)
    return [fila for _, fila in filas[:limite]]
