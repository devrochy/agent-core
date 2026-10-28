"""Ficha y detalle de Suscripciones para el cliente (HU-CRM-14, ADR-0179 decisión 3).

La ficha dice a qué plan está inscrito y en qué estado, sin importes. El detalle sí trae el valor del
período y los últimos cargos, con las claves de dinero del core.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id
from pack_subscriptions.models import Plan, Subscription, SubscriptionCharge


def _dia(valor: Any) -> str:
    return valor.strftime("%Y-%m-%d") if valor else ""


async def _suscripciones(session, ids: list[uuid.UUID], limite: int | None = None):
    q = (
        select(Subscription, Plan.nombre)
        .outerjoin(Plan, Plan.id == Subscription.plan_id)
        .where(
            Subscription.tenant_id == get_current_tenant_id(),
            Subscription.customer_id.in_(ids),
            Subscription.deleted_at.is_(None),
        )
        .order_by(Subscription.created_at.desc())
    )
    if limite:
        q = q.limit(limite)
    return (await session.execute(q)).all()


async def ficha(ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    async with get_session() as session:
        filas = await _suscripciones(session, ids)
    por_cliente: dict[uuid.UUID, list[str]] = {}
    for sub, plan in filas:
        partes = [f"plan {plan or sub.concepto or 'sin nombre'}, {sub.estado}"]
        if sub.proximo_cargo and sub.estado in ("activa", "trialing"):
            partes.append(f"próximo cargo {_dia(sub.proximo_cargo)}")
        por_cliente.setdefault(sub.customer_id, []).append(", ".join(partes))
    return {cid: "; ".join(lineas[:3]) for cid, lineas in por_cliente.items()}


async def detalle(customer_id: uuid.UUID, limite: int) -> list[dict[str, Any]]:
    async with get_session() as session:
        filas = await _suscripciones(session, [customer_id], limite)
        salida: list[dict[str, Any]] = []
        for sub, plan in filas:
            cargos = (
                await session.execute(
                    select(SubscriptionCharge)
                    .where(
                        SubscriptionCharge.tenant_id == get_current_tenant_id(),
                        SubscriptionCharge.subscription_id == sub.id,
                        SubscriptionCharge.deleted_at.is_(None),
                    )
                    .order_by(SubscriptionCharge.created_at.desc())
                    .limit(3)
                )
            ).scalars().all()
            salida.append(
                {
                    "tipo": "suscripcion",
                    "numero": sub.numero_publico,
                    "plan": plan or sub.concepto,
                    "estado": sub.estado,
                    "periodicidad": sub.periodicidad,
                    "proximo_cargo": _dia(sub.proximo_cargo),
                    "valor_periodo": float(sub.valor_periodo or 0),
                    "ultimos_cargos": [
                        {"periodo": c.periodo, "estado": c.estado, "monto": float(c.monto or 0)} for c in cargos
                    ],
                }
            )
    return salida
