"""Capacidad de pagos del core (ADR-0067): contrato compartido entre packs.

Un pack (p. ej. `pack_payments`) **registra** una `PaymentCapability`; otro
(p. ej. `pack_cobranza`) la **consume** para crear links de pago **sin importarlo**
(invariante nº5: ningún pack importa de otro). El core filtra por pack activo, con
el mismo molde que `LeadRegistry` (ADR-0056).

También expone `qr_png_data_uri`: genera un QR PNG (data URI) para que la app lo
muestre **sin dependencia de QR en el cliente**.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from core.shared.exceptions import BusinessError


class PaymentCapabilityError(BusinessError):
    """Rechazo de NEGOCIO de una capacidad (monto mínimo, config faltante).

    Subclase de `BusinessError` (HU-ARCH-18): el host la convierte en resultado
    controlado y el pack de pagos NO se auto-deshabilita. El consumidor (p. ej.
    cobranza) la muestra como mensaje accionable.

    `code` dice por qué (constantes de abajo, adenda 2026-10-10 de ADR-0067): el
    consumidor decide con él sin importar el pack de pagos (invariante nº5). Sin
    código, el consumidor no debe suponer el motivo.
    """


# Vocabulario de `PaymentCapabilityError.code`.
MONTO_BAJO_MINIMO = "monto_bajo_minimo"  # la pasarela no cobra montos tan bajos
SIN_PASARELA = "sin_pasarela"  # el negocio no configuró la pasarela
YA_PAGADO = "ya_pagado"  # la referencia ya tiene un pago aprobado
EN_REVISION = "en_revision"  # llegó un pago por otro monto: lo revisa el negocio
MONTO_NO_COINCIDE = "monto_no_coincide"  # la referencia tiene un link vigente por otro monto
NO_DISPONIBLE = "no_disponible"  # la pasarela rechazó o falló al crear el link; reintentar


@dataclass(frozen=True)
class PaymentMethod:
    """Método de pago que ofrece una capacidad (online | qr | informativo)."""

    id: str
    label: str
    kind: str = "online"


@dataclass
class PaymentLink:
    """Link de pago creado por una capacidad. `qr_payload` es lo que codifica el QR."""

    reference: str
    url: str | None = None
    qr_payload: str | None = None
    method: str | None = None


@runtime_checkable
class PaymentCapability(Protocol):
    plugin_id: str

    def methods(self) -> list[PaymentMethod]:
        """Métodos disponibles para el tenant activo."""
        ...

    async def create_link(
        self, *, reference: str, amount: float, description: str = "", method: str | None = None
    ) -> PaymentLink:
        """Crea un link de pago correlacionado con `reference` (del caller)."""
        ...


class PaymentRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, PaymentCapability] = {}

    def register(self, plugin_id: str, capability: PaymentCapability) -> None:
        """Idempotente: registrar dos veces el mismo pack reemplaza la entrada."""
        self._caps[plugin_id] = capability

    def unregister(self, plugin_id: str) -> None:
        self._caps.pop(plugin_id, None)

    def capability_for(self, enabled: set[str] | None) -> PaymentCapability | None:
        """Primera capacidad de un pack habilitado (o cualquiera si `enabled` es None)."""
        for pid, cap in self._caps.items():
            if enabled is None or pid in enabled:
                return cap
        return None

    def all(self) -> list[PaymentCapability]:
        return list(self._caps.values())


payment_registry = PaymentRegistry()


async def procesar_evento(capability: PaymentCapability, payload: dict) -> str | None:
    """Operación opcional de la capacidad (HU-SH-04, ADR-0123, riesgo R-05): aplicar un evento
    de la pasarela que llegó por otra URL. La capacidad verifica la firma con SUS credenciales.
    None si la capacidad no la implementa."""
    metodo = getattr(capability, "procesar_evento", None)
    if metodo is None:
        return None
    return str(await metodo(payload))


def qr_png_data_uri(payload: str) -> str | None:
    """QR PNG como data URI; `None` si `qrcode` no está instalado o falla."""
    if not payload:
        return None
    try:
        import qrcode
        from qrcode.image.pure import PyPNGImage

        img = qrcode.make(payload, image_factory=PyPNGImage)
        buffer = io.BytesIO()
        img.save(buffer)
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    except Exception:
        from core.shared.logger import get_logger

        get_logger(__name__).warning("qr_png_failed", exc_info=True)
        return None
