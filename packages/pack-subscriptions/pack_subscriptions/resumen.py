"""KPIs de Suscripciones para el Resumen por agente (ADR-0171): MRR, altas, churn y mora.

Reutiliza `consultas.metricas` (HU-SUB-16), la misma fuente de la pantalla de Suscripciones, así que
las cifras coinciden. Son mensuales: el «previo» es el mes anterior, no los 7 días previos.
"""

from __future__ import annotations

import uuid

from core.admin.resumen_registry import (
    Destino,
    Kpi,
    PeriodoResumen,
    ResumenProvider,
    resumen_registry,
)
from core.db.session import get_session
from pack_subscriptions import consultas

PLUGIN_ID = "pack_subscriptions"
SECCION = "pack_subscriptions:suscripciones"


async def kpis(tenant_id: uuid.UUID, p: PeriodoResumen) -> list[Kpi]:
    async with get_session() as session:
        m = await consultas.metricas(session, tenant_id, p.hasta)
    return [
        Kpi(
            "mrr",
            "Ingreso recurrente (MRR)",
            m.mrr,
            unidad="money",
            previo=m.mrr_mes_anterior,
            hint="Frente al cierre del mes anterior",
            versus="el mes anterior",
            destino=Destino("suscripciones"),
        ),
        *(
            [
                Kpi(
                    "mrr_descuento",
                    "Descuento vigente",
                    -m.mrr_descuento,
                    unidad="money",
                    hint=f"Por mes, en {m.suscripciones_con_descuento} suscripciones; el MRR no lo resta",
                    destino=Destino("suscripciones"),
                )
            ]
            if m.suscripciones_con_descuento
            else []
        ),
        Kpi(
            "altas_mes",
            "Altas del mes",
            float(m.altas_mes),
            previo=float(m.altas_mes_anterior),
            hint="Frente al mes anterior",
            versus="el mes anterior",
            destino=Destino("suscripciones.lista", {"estado": "activa"}),
        ),
        Kpi(
            "churn_mes",
            "Bajas del mes",
            m.churn_mes * 100 if m.churn_mes is not None else None,
            unidad="pct",
            polaridad="up-bad",
            hint="Sobre las vivas al inicio del mes",
            destino=Destino("suscripciones"),
        ),
        Kpi(
            "mora",
            "Cobros vencidos",
            m.mora.monto,
            unidad="money",
            polaridad="up-bad",
            hint=f"{m.mora.cargos} cobros sin pagar",
            destino=Destino("suscripciones.pagos", {"vencidos": "1"}),
        ),
    ]


resumen_registry.register(
    ResumenProvider(
        plugin_id=PLUGIN_ID,
        agent_key="suscripciones",
        nombre="Suscripciones",
        seccion=SECCION,
        kpis=kpis,
    )
)
