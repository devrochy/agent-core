"""Contrato de una notificación del centro de notificaciones (ADR-0167).

Core define solo los enums (categoría y severidad) y la forma de la spec. Cada pack es
dueño de sus textos y arma su `NotificationSpec`; core la valida al emitir. Los textos son
constantes de código (infraestructura determinista, no prompts: invariante 3 no aplica,
igual que ADR-0099) y `data` lleva solo ids, nunca PII.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CATEGORIAS: tuple[str, ...] = ("conversaciones", "ventas", "cobros", "agenda", "sistema")
SEVERIDADES: tuple[str, ...] = ("info", "success", "warning", "critical")

# Entidades a las que apunta una notificación. El cliente (panel o app) traduce el destino
# neutro `{entity, id, section}` a su propia ruta; el backend no arma rutas de cliente.
ENTIDADES: tuple[str, ...] = (
    "conversacion",
    "escalacion",
    "lead",
    "cliente",
    "pedido",
    "pago",
    "suscripcion",
    "cobro",
    "cita",
    "conexion",
    "mensaje_fallido",
    "cobranza",
)


class NotificationSpecError(ValueError):
    """La spec no cumple el contrato (categoría, severidad o campos obligatorios)."""


@dataclass(frozen=True, slots=True)
class NotificationSpec:
    event_key: str
    category: str
    severity: str
    title: str
    dedupe_key: str
    body: str = ""
    # Sección de permiso que gobierna quién la ve (`None` = solo dueños y masters).
    section: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    # Destino neutro: {"entity": ..., "id": ..., "section": ...}.
    target: dict[str, Any] | None = None

    def validate(self) -> None:
        if not self.event_key.strip():
            raise NotificationSpecError("event_key vacío")
        if self.category not in CATEGORIAS:
            raise NotificationSpecError(f"categoría inválida: {self.category!r}")
        if self.severity not in SEVERIDADES:
            raise NotificationSpecError(f"severidad inválida: {self.severity!r}")
        if not self.title.strip():
            raise NotificationSpecError("title vacío")
        if not self.dedupe_key.strip():
            raise NotificationSpecError("dedupe_key vacío")
        if self.target is not None:
            entity = self.target.get("entity")
            if entity not in ENTIDADES:
                raise NotificationSpecError(f"entidad de destino inválida: {entity!r}")
