"""Selección del proveedor de pago de la suscripción (ADR-0046).

`BILLING_PROVIDER=wompi` (default en dev/test = sandbox). Sin credenciales,
`get_billing_provider()` devuelve None y el panel avisa que la pasarela no está
configurada (nunca revienta).
"""

from __future__ import annotations

from core.billing.providers.base import BaseBillingProvider
from core.config import get_settings


def get_billing_provider() -> BaseBillingProvider | None:
    settings = get_settings()
    provider = (settings.billing_provider or "none").lower()
    if provider == "none":
        return None
    if provider == "wompi":
        if not (settings.wompi_billing_private_key and settings.wompi_billing_events_secret):
            return None
        from core.billing.providers.wompi import WompiBillingProvider

        return WompiBillingProvider(
            public_key=settings.wompi_billing_public_key,
            private_key=settings.wompi_billing_private_key,
            events_secret=settings.wompi_billing_events_secret,
            sandbox=settings.wompi_billing_sandbox,
        )
    return None
