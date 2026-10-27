"""KPIs de Cobranza para el Resumen por agente (ADR-0171): recaudo, cartera en mora y promesas.

Es el módulo completo del negocio. Los importes van en la moneda del negocio (`money`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select

from core.admin.resumen_registry import (
    Destino,
    Kpi,
    PeriodoResumen,
    ResumenProvider,
    resumen_registry,
)
from core.db.session import get_session
from pack_cobranza.models import Cuota, Pago, Promesa

PLUGIN_ID = "pack_cobranza"
SECCION = "pack_cobranza:resumen"


def _tasa(cumplidas: float, incumplidas: float) -> float | None:
    total = cumplidas + incumplidas
    return cumplidas / total * 100 if total else None


async def kpis(tenant_id: uuid.UUID, p: PeriodoResumen) -> list[Kpi]:
    async def recaudo(desde: datetime, hasta: datetime) -> float:
        return float(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Pago.valor), 0)).where(
                        Pago.tenant_id == tenant_id,
                        Pago.deleted_at.is_(None),
                        Pago.fecha_hora >= desde,
                        Pago.fecha_hora < hasta,
                    )
                )
            ).scalar()
            or 0
        )

    async def cumplimiento(desde: datetime, hasta: datetime) -> float | None:
        filas = dict(
            (
                await session.execute(
                    select(Promesa.estado, func.count(Promesa.id))
                    .where(
                        Promesa.tenant_id == tenant_id,
                        Promesa.deleted_at.is_(None),
                        Promesa.estado.in_(("cumplida", "incumplida")),
                        Promesa.fecha_prometida >= desde.date(),
                        Promesa.fecha_prometida < hasta.date(),
                    )
                    .group_by(Promesa.estado)
                )
            )
            .tuples()
            .all()
        )
        return _tasa(float(filas.get("cumplida", 0)), float(filas.get("incumplida", 0)))

    async with get_session() as session:
        mora = (
            await session.execute(
                select(func.coalesce(func.sum(Cuota.valor_esperado), 0)).where(
                    Cuota.tenant_id == tenant_id,
                    Cuota.estatus == "atrasada",
                    Cuota.deleted_at.is_(None),
                )
            )
        ).scalar() or 0
        pendientes = (
            await session.execute(
                select(func.count(Promesa.id)).where(
                    Promesa.tenant_id == tenant_id,
                    Promesa.estado == "pendiente",
                    Promesa.deleted_at.is_(None),
                )
            )
        ).scalar() or 0
        return [
            Kpi(
                "recaudo",
                "Recaudo",
                await recaudo(p.desde, p.hasta),
                unidad="money",
                previo=await recaudo(p.previo_desde, p.previo_hasta),
                hint="Últimos 7 días",
                destino=Destino("cobranza.pagos"),
            ),
            Kpi(
                "cartera_en_mora",
                "Cartera en mora",
                float(mora),
                unidad="money",
                polaridad="up-bad",
                hint="Cuotas atrasadas ahora",
                destino=Destino("cobranza.carteras", {"mora": "1"}),
            ),
            Kpi(
                "promesas_pendientes",
                "Promesas pendientes",
                float(pendientes),
                hint="Por cumplirse",
                destino=Destino("cobranza.promesas", {"estado": "pendiente"}),
            ),
            Kpi(
                "promesas_cumplidas",
                "Promesas cumplidas",
                await cumplimiento(p.desde, p.hasta),
                unidad="pct",
                previo=await cumplimiento(p.previo_desde, p.previo_hasta),
                hint="Cumplidas sobre vencidas",
                destino=Destino("cobranza.promesas", {"estado": "cumplida"}),
            ),
        ]


resumen_registry.register(
    ResumenProvider(
        plugin_id=PLUGIN_ID, agent_key="cobranza", nombre="Cobranza", seccion=SECCION, kpis=kpis
    )
)
