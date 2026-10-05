"""Búsqueda global de la app oficial (ADR-0096, HU-APP-14).

El core expone `GET /api/v1/search` y agrega resultados de providers que
registran los packs al cargarse (el core no importa de los packs, invariante 5).
Cada provider declara la sección RBAC que lo protege y devuelve resultados con
la forma uniforme `ResultadoBusqueda` para que la app arme deep links.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

SearchProvider = Callable[[uuid.UUID, str, int], Awaitable[list["ResultadoBusqueda"]]]


class ResultadoBusqueda(TypedDict):
    id: str
    tipo: str
    titulo: str
    subtitulo: str
    extra: dict[str, Any]


_REGISTRY: dict[str, dict[str, Any]] = {}


def register_search(section: str, label: str, provider: SearchProvider) -> None:
    """Registra un provider para una sección RBAC. Idempotente por sección."""
    if section in _REGISTRY:
        return
    _REGISTRY[section] = {"label": label, "provider": provider}


def search_providers() -> dict[str, dict[str, Any]]:
    """Copia del registro en orden de alta (core primero, packs después)."""
    return dict(_REGISTRY)


def escapar_ilike(q: str) -> str:
    """Escapa los comodines de `ILIKE` para que `q` sea literal."""
    return q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
