"""Registro de contribuciones de packs a la vista global de leads (ADR-0056).

Un lead es un `Customer` (ADR-0022). La vista global vive en el core
(`/admin/leads`) y muestra columnas del core **más** las que aporta cada pack
habilitado para el tenant. El core NO importa packs (invariante nº5): los packs
se registran aquí desde su `on_activate()`.

Contrato:

    lead_registry.register(
        "pack_cobranza",
        columns=[LeadColumn(key="saldo", label="Saldo"), ...],
        resolver=async def (customer_ids) -> {customer_id: {key: value}},
    )

El `resolver` recibe la lista de `customer_id` de la página y devuelve, por
cliente, los valores de sus columnas. Un provider que falla se ignora (la vista
global nunca se cae por un pack).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

LeadResolver = Callable[[list[uuid.UUID]], Awaitable[dict[uuid.UUID, dict[str, Any]]]]


@dataclass(frozen=True)
class LeadColumn:
    key: str
    label: str


@dataclass
class LeadProvider:
    plugin_id: str
    columns: list[LeadColumn] = field(default_factory=list)
    resolver: LeadResolver | None = None


class LeadRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, LeadProvider] = {}

    def register(
        self,
        plugin_id: str,
        columns: list[LeadColumn],
        resolver: LeadResolver,
    ) -> None:
        """Idempotente: registrar dos veces el mismo pack reemplaza la entrada."""
        self._providers[plugin_id] = LeadProvider(plugin_id, list(columns), resolver)

    def providers_for(self, enabled: set[str] | None) -> list[LeadProvider]:
        if enabled is None:
            return list(self._providers.values())
        return [p for pid, p in self._providers.items() if pid in enabled]

    def columns_for(self, enabled: set[str] | None) -> list[LeadColumn]:
        cols: list[LeadColumn] = []
        for provider in self.providers_for(enabled):
            cols.extend(provider.columns)
        return cols


lead_registry = LeadRegistry()
