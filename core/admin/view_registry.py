"""ViewRenderer por descriptor (Fase D, Sprint 4).

Un pack registra una vista (tabla + form JSON Schema + handlers) y el core la
sirve con HTML genérico: cero plantillas ni rutas escritas por el autor del pack.

La vista se sirve en /admin/packs/{plugin}/{view} y el guardado en
POST /admin/packs/{plugin}/{view}/guardar. El list_handler devuelve filas (dicts
con `_id` + las columnas de la tabla + los campos del form); el save_handler
recibe (record_id | None para alta nueva, values) y persiste.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field


class TableColumn(BaseModel):
    key: str
    label: str
    sortable: bool = True


class TableFilter(BaseModel):
    key: str
    label: str
    options: list[dict[str, str]] = Field(default_factory=list)


class TableDescriptor(BaseModel):
    columns: list[TableColumn]
    filters: list[TableFilter] = Field(default_factory=list)
    page_size: int = 20
    empty_label: str = "Sin registros"


class ViewDescriptor(BaseModel):
    title: str
    path: str  # relativo al admin del pack, ej: "leads"
    table: TableDescriptor
    form: dict[str, Any]  # JSON Schema del form (object + properties)
    save_label: str = "Guardar"
    add_label: str | None = None  # si la vista permite alta nueva


ListHandler = Callable[[], Awaitable[list[dict[str, Any]]]]
SaveHandler = Callable[[str | None, dict[str, Any]], Awaitable[None]]


class ViewRegistry:
    def __init__(self) -> None:
        self._views: dict[
            tuple[str, str], tuple[ViewDescriptor, ListHandler, SaveHandler]
        ] = {}

    def register(
        self,
        plugin_id: str,
        descriptor: ViewDescriptor,
        list_handler: ListHandler,
        save_handler: SaveHandler,
    ) -> None:
        key = (plugin_id, descriptor.path.strip("/"))
        self._views[key] = (descriptor, list_handler, save_handler)

    def get(self, plugin_id: str, view: str) -> tuple[ViewDescriptor, ListHandler, SaveHandler] | None:
        return self._views.get((plugin_id, view.strip("/")))

    def items(self):
        return self._views.items()


view_registry = ViewRegistry()