"""Contrato del proveedor de pago de la SUSCRIPCIÓN de plataforma (ADR-0046).

Vive dentro de `core/billing/providers` (no en `pack_payments`): el cobro del
plan lo hace la plataforma (tenant → nosotros), no el negocio del tenant
(tenant → sus clientes). Son dominios distintos, con credenciales y tablas
distintas. Core no puede importar un pack (regla de capas), así que el cliente
Wompi se replica aquí deliberadamente — ver ADR-0046.

Forma calcada de `pack_payments/providers/base.py` (un método por operación).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class BillingProviderError(Exception):
    """Falla real del proveedor (red, 5xx) — no un rechazo de negocio."""


class BillingRequestRejected(Exception):
    """El proveedor RECHAZÓ la solicitud por una regla de negocio (monto mínimo,
    configuración faltante). No es una falla del sistema: el caller muestra un
    mensaje accionable, nunca apaga nada."""


@dataclass
class PaymentLink:
    provider_reference: str
    url: str


@dataclass
class BillingWebhookEvent:
    provider_reference: str  # id del link (lo que guardamos al crearlo)
    status: str  # normalizado: pendiente | aprobado | rechazado
    amount_in_cents: int | None
    raw: dict[str, Any]


class BaseBillingProvider(ABC):
    provider_name: str = "base"

    @abstractmethod
    async def create_payment_link(
        self, *, reference: str, amount_cop: float, expires_in_hours: int = 24
    ) -> PaymentLink:
        """Crea un link de pago de un solo uso, correlacionado con `reference`."""

    @abstractmethod
    def verify_webhook_signature(self, payload: dict[str, Any]) -> bool:
        """Verifica la firma del webhook. NUNCA procesar sin verificar."""

    @abstractmethod
    def parse_webhook_event(self, payload: dict[str, Any]) -> BillingWebhookEvent:
        """Interpreta un webhook ya verificado como legítimo."""
