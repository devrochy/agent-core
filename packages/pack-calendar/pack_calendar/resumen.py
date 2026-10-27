"""KPIs de Agenda para el Resumen por agente (ADR-0171): citas de hoy, próximas, agendadas y canceladas.

Es el módulo completo del negocio. «Hoy» se mide en la zona horaria de la agenda del negocio.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from core.admin.resumen_registry import (
    Destino,
    Kpi,
    PeriodoResumen,
    ResumenProvider,
    resumen_registry,
)
from core.db.session import get_session
from core.plugin_manager import plugin_manager
from pack_calendar.models import Appointment

PLUGIN_ID = "pack_calendar"
SECCION = "pack_calendar:citas"
_NO_ATENDIDA = ("cancelled", "no_show")


async def _zona() -> ZoneInfo:
    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
        return ZoneInfo(getattr(config, "timezone", None) or "America/Bogota")
    except Exception:
        return ZoneInfo("America/Bogota")


async def kpis(tenant_id: uuid.UUID, p: PeriodoResumen) -> list[Kpi]:
    a = Appointment
    base = [a.tenant_id == tenant_id, a.deleted_at.is_(None)]
    zona = await _zona()
    inicio_hoy = p.hasta.astimezone(zona).replace(hour=0, minute=0, second=0, microsecond=0)
    fin_hoy = inicio_hoy + timedelta(days=1)

    async def contar(*condiciones) -> float:  # type: ignore[no-untyped-def]
        return float(
            (await session.execute(select(func.count(a.id)).where(*base, *condiciones))).scalar()
            or 0
        )

    async with get_session() as session:
        return [
            Kpi(
                "citas_hoy",
                "Citas de hoy",
                await contar(
                    a.status == "scheduled", a.start_at >= inicio_hoy, a.start_at < fin_hoy
                ),
                hint="Agendadas para hoy",
                destino=Destino("calendario.citas", {"periodo": "hoy"}),
            ),
            Kpi(
                "citas_proximas",
                "Próximos 7 días",
                await contar(
                    a.status == "scheduled",
                    a.start_at >= p.hasta,
                    a.start_at < p.hasta + timedelta(days=7),
                ),
                hint="Citas por venir",
                destino=Destino("calendario.citas", {"periodo": "proximos_7d"}),
            ),
            Kpi(
                "citas_agendadas",
                "Citas agendadas",
                await contar(a.created_at >= p.desde, a.created_at < p.hasta),
                previo=await contar(a.created_at >= p.previo_desde, a.created_at < p.previo_hasta),
                hint="Creadas en los últimos 7 días",
                destino=Destino("calendario.citas"),
            ),
            Kpi(
                "citas_no_atendidas",
                "Canceladas o sin asistir",
                await contar(
                    a.status.in_(_NO_ATENDIDA), a.updated_at >= p.desde, a.updated_at < p.hasta
                ),
                previo=await contar(
                    a.status.in_(_NO_ATENDIDA),
                    a.updated_at >= p.previo_desde,
                    a.updated_at < p.previo_hasta,
                ),
                polaridad="up-bad",
                destino=Destino("calendario.citas", {"estado": "cancelled"}),
            ),
        ]


resumen_registry.register(
    ResumenProvider(
        plugin_id=PLUGIN_ID, agent_key="agenda", nombre="Agenda", seccion=SECCION, kpis=kpis
    )
)
