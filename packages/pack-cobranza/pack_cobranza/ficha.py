"""Ficha y detalle de Cobranza para el cliente (HU-CRM-14, ADR-0179 decisión 3).

La ficha solo dice si hay cartera ligada, cuántas cuotas están atrasadas y si hay una promesa de pago
pendiente. Sin saldos ni valores: esos salen solo por la tool de historial.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select

from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id
from pack_cobranza.models import Cliente, Cuota, Prestamo, Promesa

_PRESTAMO_VIVO = "vigente"


def _dia(valor: Any) -> str:
    return valor.strftime("%Y-%m-%d") if valor else ""


async def ficha(ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    tid = get_current_tenant_id()
    async with get_session() as session:
        clientes = (
            await session.execute(
                select(Cliente.id, Cliente.customer_id).where(
                    Cliente.tenant_id == tid, Cliente.customer_id.in_(ids), Cliente.deleted_at.is_(None)
                )
            )
        ).all()
        if not clientes:
            return {}
        cliente_ids = [c for c, _ in clientes]
        prestamos = dict(
            (
                await session.execute(
                    select(Prestamo.cliente_id, func.count(Prestamo.id))
                    .where(
                        Prestamo.tenant_id == tid,
                        Prestamo.cliente_id.in_(cliente_ids),
                        Prestamo.estatus == _PRESTAMO_VIVO,
                        Prestamo.deleted_at.is_(None),
                    )
                    .group_by(Prestamo.cliente_id)
                )
            ).all()
        )
        atrasadas = dict(
            (
                await session.execute(
                    select(Prestamo.cliente_id, func.count(Cuota.id))
                    .join(Prestamo, Prestamo.id == Cuota.prestamo_id)
                    .where(
                        Cuota.tenant_id == tid,
                        Prestamo.cliente_id.in_(cliente_ids),
                        Cuota.estatus == "atrasada",
                        Cuota.deleted_at.is_(None),
                        Prestamo.deleted_at.is_(None),
                    )
                    .group_by(Prestamo.cliente_id)
                )
            ).all()
        )
        promesas = dict(
            (
                await session.execute(
                    select(Promesa.cliente_id, func.min(Promesa.fecha_prometida))
                    .where(
                        Promesa.tenant_id == tid,
                        Promesa.cliente_id.in_(cliente_ids),
                        Promesa.estado == "pendiente",
                        Promesa.deleted_at.is_(None),
                    )
                    .group_by(Promesa.cliente_id)
                )
            ).all()
        )
    salida: dict[uuid.UUID, str] = {}
    for cliente_id, customer_id in clientes:
        partes = [f"cliente de cartera con {prestamos.get(cliente_id, 0)} préstamo(s) vigente(s)"]
        if atrasadas.get(cliente_id):
            partes.append(f"{atrasadas[cliente_id]} cuota(s) atrasada(s)")
        if promesas.get(cliente_id):
            partes.append(f"promesa de pago pendiente para el {_dia(promesas[cliente_id])}")
        salida[customer_id] = "; ".join(partes)
    return salida


async def detalle(customer_id: uuid.UUID, limite: int) -> list[dict[str, Any]]:
    tid = get_current_tenant_id()
    async with get_session() as session:
        cliente = (
            await session.execute(
                select(Cliente.id).where(
                    Cliente.tenant_id == tid, Cliente.customer_id == customer_id, Cliente.deleted_at.is_(None)
                )
            )
        ).scalars().first()
        if cliente is None:
            return []
        prestamos = (
            await session.execute(
                select(Prestamo)
                .where(Prestamo.tenant_id == tid, Prestamo.cliente_id == cliente, Prestamo.deleted_at.is_(None))
                .order_by(Prestamo.created_at.desc())
                .limit(limite)
            )
        ).scalars().all()
        salida: list[dict[str, Any]] = []
        for p in prestamos:
            cuotas = (
                await session.execute(
                    select(Cuota.estatus, func.count(Cuota.id))
                    .where(Cuota.tenant_id == tid, Cuota.prestamo_id == p.id, Cuota.deleted_at.is_(None))
                    .group_by(Cuota.estatus)
                )
            ).all()
            salida.append(
                {
                    "tipo": "prestamo",
                    "estado": p.estatus,
                    "otorgado": _dia(p.fecha_otorgado),
                    "valor": float(p.valor or 0),
                    "saldo": float(p.saldo or 0),
                    "cuotas": {estatus: n for estatus, n in cuotas},
                }
            )
        promesas = (
            await session.execute(
                select(Promesa)
                .where(Promesa.tenant_id == tid, Promesa.cliente_id == cliente, Promesa.deleted_at.is_(None))
                .order_by(Promesa.fecha_prometida.desc())
                .limit(limite)
            )
        ).scalars().all()
        salida += [
            {"tipo": "promesa", "estado": q.estado, "fecha": _dia(q.fecha_prometida), "valor": float(q.valor_prometido or 0)}
            for q in promesas
        ]
    return salida[:limite]
