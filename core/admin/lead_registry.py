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

Filtros (HU-UX-16): un pack puede declarar `filters` (clave, etiqueta y opciones
cerradas) y un `filter_query(clave, valor)` que devuelve un `SELECT` de una sola
columna con los `customer_id` que cumplen. El core lo usa como `Customer.id IN (…)`
sin conocer las tablas del pack. Una clave o un valor fuera de las opciones se
rechaza antes de llamar al pack.

Línea de tiempo (HU-CRM-04, ADR-0147, enmienda de esta ADR): un pack puede aportar `timeline`,
`async (customer_id, cursor, limit) -> [TimelineEvent]` con sus eventos del cliente del más nuevo
al más viejo y estrictamente anteriores al `cursor` `(at, source, id)`. `antes_del_cursor` arma
ese filtro en SQL: con varias fuentes hay empates de hora y un cursor solo por fecha perdería o
repetiría eventos al paginar.

Edición desde la ficha (ADR-0149, enmienda de esta ADR): un pack puede aportar `edicion`, con los
`campos` que una persona puede cambiar en la ficha del cliente (opciones cerradas o texto corto),
`leer(customer_id)` con sus valores actuales (códigos, no etiquetas) y `escribir(customer_id,
valores)`. El core valida claves y valores contra `campos` antes de llamar al pack, así que el
pack solo recibe valores admitidos y el core sigue sin conocer sus tablas.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import String, and_, cast, or_

LeadResolver = Callable[[list[uuid.UUID]], Awaitable[dict[uuid.UUID, dict[str, Any]]]]
LeadFilterQuery = Callable[[str, str], Awaitable[Any]]  # (clave, valor) -> Select[customer_id]

Cursor = tuple[datetime, str, str]  # (at, source, id) del último evento de la página anterior


@dataclass(frozen=True)
class TimelineEvent:
    """Un hecho de la historia del cliente. `link_kind`/`link_id` dicen a qué lleva (p. ej.
    `conversation`), sin que el panel conozca las rutas del pack."""

    at: datetime  # con zona horaria
    source: str  # fuente estable: "core.conversacion", "pack_commerce.pedido"…
    id: str  # id estable dentro de la fuente (desempata el cursor)
    kind: str  # conversacion | escalacion | dato | nota | cotizacion | pedido | cita…
    title: str
    detail: str | None = None
    link_kind: str | None = None
    link_id: str | None = None


LeadTimeline = Callable[[uuid.UUID, Cursor | None, int], Awaitable[list[TimelineEvent]]]


def antes_del_cursor(col_at: Any, col_id: Any, source: str, cursor: Cursor | None) -> Any:
    """Filtro SQL de los eventos de `source` que van después del cursor (orden at, source, id desc)."""
    if cursor is None:
        return True
    c_at, c_source, c_id = cursor
    if source > c_source:
        return col_at < c_at
    if source < c_source:
        return col_at <= c_at
    return or_(col_at < c_at, and_(col_at == c_at, cast(col_id, String) < c_id))


@dataclass(frozen=True)
class LeadColumn:
    key: str
    label: str


@dataclass(frozen=True)
class LeadFilter:
    key: str
    label: str
    options: tuple[tuple[str, str], ...]  # (valor, etiqueta)


@dataclass(frozen=True)
class LeadCampoEditable:
    key: str
    label: str
    tipo: Literal["opciones", "texto"]
    options: tuple[tuple[str, str], ...] = ()  # (valor, etiqueta) si tipo == "opciones"
    max_length: int = 200


@dataclass(frozen=True)
class LeadEdicion:
    """Campos del pack que se editan desde la ficha (ADR-0149)."""

    campos: tuple[LeadCampoEditable, ...]
    leer: Callable[[uuid.UUID], Awaitable[dict[str, Any]]]
    escribir: Callable[[uuid.UUID, dict[str, str]], Awaitable[None]]


@dataclass
class LeadProvider:
    plugin_id: str
    columns: list[LeadColumn] = field(default_factory=list)
    resolver: LeadResolver | None = None
    filters: list[LeadFilter] = field(default_factory=list)
    filter_query: LeadFilterQuery | None = None
    timeline: LeadTimeline | None = None
    edicion: LeadEdicion | None = None
    # Área del cliente a la que pertenece la data del pack (Ventas, Cobranza, Agenda…).
    # El panel la usa para agrupar columnas y mostrar el icono del agente; el core no
    # conoce los nombres comerciales de los packs. Sin esto, la clave es el `plugin_id`.
    agent_key: str | None = None
    agent_name: str | None = None
    icon: str | None = None


class LeadRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, LeadProvider] = {}

    def register(
        self,
        plugin_id: str,
        columns: list[LeadColumn],
        resolver: LeadResolver | None,
        filters: list[LeadFilter] | None = None,
        filter_query: LeadFilterQuery | None = None,
        timeline: LeadTimeline | None = None,
        edicion: LeadEdicion | None = None,
        *,
        agent_key: str | None = None,
        agent_name: str | None = None,
        icon: str | None = None,
    ) -> None:
        """Idempotente: registrar dos veces el mismo pack reemplaza la entrada."""
        self._providers[plugin_id] = LeadProvider(
            plugin_id,
            list(columns),
            resolver,
            list(filters or []),
            filter_query,
            timeline,
            edicion,
            agent_key,
            agent_name,
            icon,
        )

    def providers_for(self, enabled: set[str] | None) -> list[LeadProvider]:
        if enabled is None:
            return list(self._providers.values())
        return [p for pid, p in self._providers.items() if pid in enabled]

    def columns_for(self, enabled: set[str] | None) -> list[LeadColumn]:
        cols: list[LeadColumn] = []
        for provider in self.providers_for(enabled):
            cols.extend(provider.columns)
        return cols

    def filter_for(self, enabled: set[str] | None, key: str) -> tuple[LeadProvider, LeadFilter] | None:
        """El provider habilitado que declara el filtro `key`, o `None`."""
        for provider in self.providers_for(enabled):
            for filtro in provider.filters:
                if filtro.key == key and provider.filter_query is not None:
                    return provider, filtro
        return None


lead_registry = LeadRegistry()
