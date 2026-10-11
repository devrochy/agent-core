"""Contrato de proveedor de pagos: Wompi es el primero, no el único (ADR-0023).

Vive DENTRO de pack-payments (no en packages/contracts ni en core/) porque
hoy solo este pack lo consume — si otro pack necesitara pagos en el futuro,
ahí se decide si sube al core. Mismo criterio de forma que
`core/llm/base.py::BaseLLMProvider` (un método por operación, sin exponer
detalles del proveedor concreto al resto del pack).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class PaymentRequestRejected(Exception):
    """El proveedor RECHAZÓ la solicitud por una razón de negocio (monto fuera
    de rango, dato faltante, etc.) — no es una falla del plugin.

    Encontrado en vivo (tenant yopoldo, 2026-09-11): `create_payment_link`
    lanzaba `PluginError` para CUALQUIER rechazo 4xx de Wompi, incluida una
    simple validación de negocio ("La base de la transacción debe ser igual
    o mayor a 150000"). Eso llega sin capturar hasta
    `core/orchestrator/loop.py::_execute_tool_safely`, que trata TODO
    `PluginError` como una falla real del plugin y llama
    `plugin_manager.auto_disable()` -- apagando `pack_payments` para TODOS
    los tenants por un rechazo de negocio totalmente esperable, exactamente
    el mismo patrón ya corregido para `pack_commerce` (IDs inválidos). El
    handler de la tool debe atrapar ESTA excepción específica y devolver un
    error recuperable, nunca dejarla llegar a `PluginError`.
    """


class MontoBajoMinimo(PaymentRequestRejected):
    """El monto es menor al mínimo que el proveedor acepta en este ambiente: el
    consumidor ofrece otro medio de pago en vez de reintentar."""


@dataclass
class PaymentLinkResult:
    provider_reference: str
    payment_link_url: str


@dataclass
class WebhookEvent:
    """Resultado de interpretar un webhook ya verificado como legítimo."""

    reference: str  # la que nosotros le dimos al crear el link
    provider_reference: str
    status: str  # normalizado a pendiente|aprobado|rechazado (nunca el vocabulario crudo del proveedor)
    raw: dict[str, Any]
    # Lo que el proveedor dice que se pagó, en unidades de la moneda (no
    # centavos). `None` si no viene o no está cubierto por la firma: nunca se
    # confía en un monto que el proveedor no firmó.
    amount: float | None = None


class BasePaymentProvider(ABC):
    provider_name: str = "base"

    @abstractmethod
    async def create_payment_link(
        self, *, reference: str, amount: float, currency: str, expires_in_hours: int
    ) -> PaymentLinkResult:
        """Crea un link de pago de un solo uso, correlacionado con `reference`."""

    @abstractmethod
    def verify_webhook_signature(self, payload: dict[str, Any]) -> bool:
        """True si el payload es legítimo (firma del proveedor válida)."""

    @abstractmethod
    def parse_webhook_event(self, payload: dict[str, Any]) -> WebhookEvent:
        """Solo llamar tras `verify_webhook_signature` — no valida nada por su cuenta."""
