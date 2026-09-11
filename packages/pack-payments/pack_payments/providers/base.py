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
