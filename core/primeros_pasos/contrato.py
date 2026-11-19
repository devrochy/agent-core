"""Lo que una habilidad aporta a los primeros pasos (HU-SH-06, ADR-0124).

El paso «catálogo o servicios» se cumple con lo que el negocio cargó en sus habilidades: los
productos de comercio, los tipos de cita de la agenda o los clientes de cobranza. El core no
lee las tablas de los packs (misma regla que ADR-0121): cada pack implementa
`BaseModule.contenido_cargado(tenant_id)` y devuelve un `ContenidoCargado`.

Este módulo es lo único de `core.primeros_pasos` que un pack puede importar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

TipoDeContenido = Literal["catalogo", "servicios", "cartera"]


@dataclass(frozen=True)
class ContenidoCargado:
    """Lo que el negocio cargó en una habilidad para que el asistente tenga qué ofrecer."""

    tipo: TipoDeContenido
    # Elementos vigentes (productos a la venta, tipos de cita activos, clientes).
    cantidad: int
    # Cuándo se cargó el primero: el embudo mide el tiempo desde el alta hasta aquí.
    primero_en: datetime | None
    # Sección del RBAC con la que se carga (ADR-0047/0063): el panel muestra la acción solo a
    # quien puede usarla.
    seccion: str


__all__ = ["ContenidoCargado", "TipoDeContenido"]
