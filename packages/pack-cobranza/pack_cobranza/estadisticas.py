"""Conteos de la cartera para la vista de estadísticas (app del gestor).

Extraído para que el endpoint (`api_gestor.py`) y el snapshot diario (`jobs.py`)
calculen lo mismo: el endpoint devuelve los conteos de hoy y el snapshot guarda
los de ayer, con lo que `anterior`/`deltas` quedan reales.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select

from pack_cobranza.models import Cliente, Cuota, Prestamo, Visita


async def conteos_cartera(session, tenant_id: uuid.UUID, cartera_id: uuid.UUID) -> dict[str, int]:
    hoy = date.today()
    clientes = list((await session.execute(select(Cliente).where(Cliente.tenant_id == tenant_id, Cliente.cartera_id == cartera_id, Cliente.deleted_at.is_(None)))).scalars().all())
    prestamos = list((await session.execute(select(Prestamo).where(Prestamo.tenant_id == tenant_id, Prestamo.cartera_id == cartera_id, Prestamo.deleted_at.is_(None)))).scalars().all())
    prestamo_ids = [p.id for p in prestamos]
    cuotas = list((await session.execute(select(Cuota).where(Cuota.tenant_id == tenant_id, Cuota.prestamo_id.in_(prestamo_ids), Cuota.deleted_at.is_(None)))).scalars().all()) if prestamo_ids else []
    visitas_hoy = {v.cliente_id for v in (await session.execute(select(Visita).where(Visita.tenant_id == tenant_id, Visita.cartera_id == cartera_id, Visita.fecha == hoy, Visita.deleted_at.is_(None)))).scalars().all()}
    por_prestamo = {p.id: p.cliente_id for p in prestamos}
    por_cliente: dict[uuid.UUID, list[Cuota]] = {}
    for cu in cuotas:
        cliente_id = por_prestamo.get(cu.prestamo_id)
        if cliente_id is not None:
            por_cliente.setdefault(cliente_id, []).append(cu)

    def _fecha(v: Any) -> str:
        return v.isoformat()[:10] if v is not None else ""

    return {
        "totalClientes": len(clientes),
        "clientesAtrasados": sum(1 for cus in por_cliente.values() if any(c.estatus == "atrasada" for c in cus)),
        "clientesVencidos": sum(1 for cus in por_cliente.values() if any(c.estatus != "pagada" and c.fecha_vencimiento == hoy for c in cus)),
        "sinVisitaHoy": sum(1 for c in clientes if c.id not in visitas_hoy),
        "sinVisitaDesdeUltimaLiquidada": sum(1 for c in clientes if c.id not in visitas_hoy),
        "conPrestamosNuevos": sum(1 for p in prestamos if p.fecha_otorgado == hoy),
        "conMasDeUnPrestamo": sum(1 for cus in por_cliente.values() if len({c.prestamo_id for c in cus}) > 1),
        "clientesNuevos": sum(1 for c in clientes if _fecha(c.created_at) == hoy.isoformat()),
    }
