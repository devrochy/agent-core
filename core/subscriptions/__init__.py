"""Capacidad de suscripciones del core (ADR-0083): puente entre packs.

Un pack (p. ej. `pack_subscriptions`) **registra** una `SubscriptionCapability`;
`pack_commerce` la **consume** (sin importarla, invariante nº5): al pagarse un
pedido que incluye un plan (variante con `billing_period`), commerce entrega un
**snapshot** del plan (nombre, precio, moneda, periodicidad, trial) y el pack
activa la suscripción del `Customer`. Mismo molde que `PaymentRegistry` (ADR-0067).
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable


class SubscriptionError(Exception):
    """Rechazo de NEGOCIO de una capacidad (plan inexistente, ya suscrito…)."""


@runtime_checkable
class SubscriptionCapability(Protocol):
    plugin_id: str

    async def activate_for_customer(
        self,
        *,
        customer_id: uuid.UUID,
        variant_id: str,
        plan: dict[str, Any],
        reference: str = "",
    ) -> dict[str, Any]:
        """Activa/crea la suscripción del `Customer` a un plan (idempotente).

        `plan` es el snapshot que aporta el caller (commerce/admin): nombre,
        precio, moneda, periodicidad, trial_dias.
        """
        ...


class SubscriptionRegistry:
    def __init__(self) -> None:
        self._caps: dict[str, SubscriptionCapability] = {}

    def register(self, plugin_id: str, capability: SubscriptionCapability) -> None:
        """Idempotente: registrar dos veces el mismo pack reemplaza la entrada."""
        self._caps[plugin_id] = capability

    def unregister(self, plugin_id: str) -> None:
        self._caps.pop(plugin_id, None)

    def capability_for(self, enabled: set[str] | None) -> SubscriptionCapability | None:
        """Primera capacidad de un pack habilitado (o cualquiera si `enabled` es None)."""
        for pid, cap in self._caps.items():
            if enabled is None or pid in enabled:
                return cap
        return None

    async def activate_for_customer(
        self,
        *,
        customer_id: uuid.UUID,
        variant_id: str,
        plan: dict[str, Any],
        reference: str = "",
        enabled: set[str] | None = None,
    ) -> dict[str, Any] | None:
        cap = self.capability_for(enabled)
        if cap is None:
            return None
        return await cap.activate_for_customer(
            customer_id=customer_id, variant_id=variant_id, plan=plan, reference=reference
        )


subscription_registry = SubscriptionRegistry()