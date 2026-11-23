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


class MontoBajoElMinimo(BillingRequestRejected):
    """El monto es menor que el mínimo de Wompi del ambiente (`core.payments.wompi_minimo`): se
    rechaza antes de la red. En el sandbox (150.000 COP) deja fuera a los paquetes de recarga."""

    def __init__(self, mensaje: str, *, minimo_cop: int) -> None:
        self.minimo_cop = minimo_cop
        super().__init__(mensaje)


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
    # Id de la transacción del proveedor, solo si la firma lo cubre: un mismo pago aprobado no se aplica dos veces.
    transaction_id: str | None = None


class BaseBillingProvider(ABC):
    provider_name: str = "base"

    @abstractmethod
    async def create_payment_link(
        self, *, reference: str, amount_cop: float, expires_in_hours: int = 24,
        plan_name: str = "", concepto: str = "plan", descripcion: str | None = None,
    ) -> PaymentLink:
        """Crea un link de pago de un solo uso, correlacionado con `reference`.

        `plan_name` se usa en la descripción visible al cliente (nunca un nombre
        técnico como "agent-core", HU-REP-04). `concepto` es `plan` (suscripción) o
        `recarga` (paquete de créditos, ADR-0209 §4); `descripcion`, si viene, reemplaza
        la descripción por defecto del concepto (la arma el servidor, nunca el cliente).
        """

    @abstractmethod
    def verify_webhook_signature(self, payload: dict[str, Any]) -> bool:
        """Verifica la firma del webhook. NUNCA procesar sin verificar."""

    @abstractmethod
    def parse_webhook_event(self, payload: dict[str, Any]) -> BillingWebhookEvent:
        """Interpreta un webhook ya verificado como legítimo."""
