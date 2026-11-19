"""Registro de KPIs de módulo para el Resumen por agente (ADR-0171).

El core no conoce las tablas de los packs: cada pack registra un `ResumenProvider` (igual que
`lead_registry` con `LeadProvider`) que calcula los KPIs de su módulo para un período, y el Resumen
(`core/metricas/resumen_agentes.py`) los agrupa bajo el agente cuya `agent_key` coincide con la
clave de su plantilla (ventas, agenda, cobranza, suscripciones).

Los enlaces (`Destino`) son **semánticos**, no URLs: el backend no sabe cómo se llaman las pantallas
del panel ni de la app; cada cliente traduce `ruta` + `params` a su navegación.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

Unidad = Literal["count", "pct", "usd", "money", "ms"]
Polaridad = Literal["up-good", "up-bad"]


@dataclass(frozen=True)
class Destino:
    """A dónde lleva un KPI: una ruta lógica (`comercio.pedidos`) y sus filtros."""

    ruta: str
    params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PeriodoResumen:
    """Ventana actual y la inmediata anterior, de la misma duración (7, 15 o 30 d)."""

    desde: datetime
    hasta: datetime
    previo_desde: datetime
    previo_hasta: datetime

    @property
    def dias(self) -> int:
        """Días de la ventana, para los textos («Últimos N días») y la serie diaria."""
        return max(1, round((self.hasta - self.desde) / timedelta(days=1)))


@dataclass(frozen=True)
class Kpi:
    id: str
    label: str
    valor: float | None
    unidad: Unidad = "count"
    # Valor del período anterior; `None` si el KPI es una foto de hoy (sin comparación).
    previo: float | None = None
    polaridad: Polaridad = "up-good"
    hint: str | None = None
    destino: Destino | None = None
    # Contra qué se compara `previo` («el mes anterior»); sin él, la ventana anterior del Resumen.
    versus: str | None = None

    @property
    def delta_pct(self) -> float | None:
        """Variación porcentual contra el período anterior; `None` sin valor, sin previo o previo 0."""
        if self.valor is None or self.previo is None or self.previo == 0:
            return None
        return (self.valor - self.previo) / abs(self.previo) * 100


KpisDelModulo = Callable[[uuid.UUID, PeriodoResumen], Awaitable[list[Kpi]]]


@dataclass(frozen=True)
class ResumenProvider:
    plugin_id: str
    # Clave de la plantilla del agente que gestiona el módulo (`agents.key`).
    agent_key: str
    nombre: str
    # Sección de permisos que exige ver el módulo (la misma de su pantalla de Resumen).
    seccion: str
    kpis: KpisDelModulo


class ResumenRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ResumenProvider] = {}

    def register(self, provider: ResumenProvider) -> None:
        """Idempotente: registrar dos veces el mismo pack reemplaza la entrada."""
        self._providers[provider.plugin_id] = provider

    def providers_for(self, enabled: set[str] | None) -> list[ResumenProvider]:
        if enabled is None:
            return list(self._providers.values())
        return [p for pid, p in self._providers.items() if pid in enabled]


resumen_registry = ResumenRegistry()
