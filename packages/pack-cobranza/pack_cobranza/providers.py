"""Contribución del pack a la vista global de leads (ADR-0056).

Aporta las columnas de cartera (saldo, mora, riesgo) al lead global del core,
para los clientes de cobranza que además son `Customer` del core (vinculados por
`Cliente.customer_id`). El registro es a nivel de módulo: el core filtra por
pack habilitado al renderizar la vista.

Importar este módulo (lo hace `module.py`) registra el provider.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from core.admin.lead_registry import LeadColumn, lead_registry
from core.db.session import get_session
from core.shared.tenant import get_current_tenant_id
from pack_cobranza.models import Cliente, Cuota, Prestamo

PLUGIN_ID = "pack_cobranza"


async def _resolver(customer_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    if not customer_ids:
        return {}
    tid = get_current_tenant_id()
    out: dict[uuid.UUID, dict[str, Any]] = {}
    async with get_session() as session:
        clientes = (
            await session.execute(
                select(Cliente).where(
                    Cliente.tenant_id == tid,
                    Cliente.customer_id.in_(customer_ids),
                    Cliente.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        for cliente in clientes:
            if cliente.customer_id is None:
                continue
            prestamos = (
                await session.execute(
                    select(Prestamo.id).where(
                        Prestamo.tenant_id == tid,
                        Prestamo.cliente_id == cliente.id,
                        Prestamo.estatus == "vigente",
                    )
                )
            ).scalars().all()
            saldo = Decimal(0)
            mora = 0
            if prestamos:
                cuotas = (
                    await session.execute(
                        select(Cuota).where(
                            Cuota.tenant_id == tid,
                            Cuota.prestamo_id.in_(prestamos),
                            Cuota.estatus.in_(("pendiente", "atrasada")),
                        )
                    )
                ).scalars().all()
                saldo = sum((q.valor_esperado for q in cuotas), Decimal(0))
                mora = max(
                    ((date.today() - q.fecha_vencimiento).days for q in cuotas if q.fecha_vencimiento < date.today()),
                    default=0,
                )
            out[cliente.customer_id] = {
                "saldo": float(saldo),
                "mora": mora,
                "riesgo": cliente.color_riesgo,
            }
    return out


lead_registry.register(
    PLUGIN_ID,
    columns=[
        LeadColumn(key="saldo", label="Saldo"),
        LeadColumn(key="mora", label="Mora (días)"),
        LeadColumn(key="riesgo", label="Riesgo"),
    ],
    resolver=_resolver,
)
