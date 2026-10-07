"""Capacidad de suscripciones del core — contrato único v1 (HU-SUB-01, ADR-0106).

Un proveedor (`pack_subscriptions` hoy; `pack_cobranza` con HU-SUB-15) registra
una `SubscriptionCapability` con las 8 operaciones del contrato. El core
despacha las tools `sub_*` al proveedor del negocio y commerce consume el
registro para activar la suscripción al pagarse un pedido.

Selección del proveedor: `capability_for_tenant` resuelve entre los proveedores
habilitados para el tenant; dos habilitados sin elección explícita lanzan
`SubscriptionProviderAmbiguity` (configuración visible en el panel).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict, runtime_checkable

from core.shared.exceptions import BusinessError
from core.shared.logger import get_logger

log = get_logger(__name__)


class SubscriptionError(BusinessError):
    """Rechazo de NEGOCIO de una capacidad (HU-ARCH-18): resultado controlado,
    el proveedor SIGUE habilitado. `code` es estable para el golden set."""

    def __init__(self, message: str, *, code: str = "error_suscripcion") -> None:
        super().__init__(message, code=code)


class SubscriptionProviderAmbiguity(SubscriptionError):
    """Dos proveedores habilitados sin elección explícita (config del tenant)."""

    def __init__(self, provider_ids: list[str]) -> None:
        self.provider_ids = sorted(provider_ids)
        super().__init__(
            "Dos proveedores de suscripción habilitados sin elección explícita: "
            f"{', '.join(self.provider_ids)}. Elige uno en la configuración del negocio.",
            code="proveedor_ambiguo",
        )


class PlanSnapshot(TypedDict, total=False):
    """Snapshot del plan que arma el SERVIDOR (commerce/admin), nunca el LLM."""

    plan_code: str
    nombre: str
    precio: float
    moneda: str
    periodicidad: str  # mensual | trimestral | anual
    trial_dias: int    # solo Free tiene prueba (D-16)
    entitlements: list[str]


class SubscriptionView(TypedDict, total=False):
    """Vista canónica de una suscripción (para tools y panel)."""

    subscription_id: str
    plan_code: str
    plan: str
    estado: str
    valor_periodo: float
    moneda: str
    proximo_cargo: str | None
    trial_termina: str | None
    cargos: list[dict[str, Any]]


class ChargeLink(TypedDict, total=False):
    url: str
    referencia: str
    periodo: str
    monto: float


@runtime_checkable
class SubscriptionCapability(Protocol):
    provider_id: str

    async def activate(
        self,
        *,
        customer_id: uuid.UUID,
        variant_id: str,
        plan: PlanSnapshot,
        reference: str = "",
    ) -> SubscriptionView: ...

    async def get(self, subscription_id: uuid.UUID) -> SubscriptionView: ...

    async def list_for_customer(self, customer_id: uuid.UUID) -> list[SubscriptionView]: ...

    async def pause(
        self, subscription_id: uuid.UUID, *, reason: str = "", actor: str = ""
    ) -> SubscriptionView: ...

    async def resume(
        self, subscription_id: uuid.UUID, *, actor: str = ""
    ) -> SubscriptionView: ...

    async def cancel(
        self, subscription_id: uuid.UUID, *, reason: str = "", actor: str = ""
    ) -> SubscriptionView: ...

    async def change_plan(
        self,
        subscription_id: uuid.UUID,
        *,
        variant_id: str,
        plan: PlanSnapshot,
    ) -> SubscriptionView: ...

    async def charge_link(self, subscription_id: uuid.UUID, *, period: str = "") -> ChargeLink: ...


@dataclass
class SubscriptionRegistry:
    _caps: dict[str, SubscriptionCapability] = field(default_factory=dict)

    def register(self, provider_id: str, capability: SubscriptionCapability) -> None:
        """Idempotente: registrar dos veces el mismo proveedor reemplaza la entrada."""
        self._caps[provider_id] = capability

    def unregister(self, provider_id: str) -> None:
        self._caps.pop(provider_id, None)

    def get(self, provider_id: str) -> SubscriptionCapability | None:
        return self._caps.get(provider_id)

    def registered_provider_ids(self) -> list[str]:
        return sorted(self._caps)

    def capability_for(self, enabled: set[str] | None) -> SubscriptionCapability | None:
        """Primera capacidad de un proveedor habilitado (o cualquiera si `enabled` es None).

        Compatibilidad con el consumo de commerce previo al ADR-0106.
        """
        for pid, cap in self._caps.items():
            if enabled is None or pid in enabled:
                return cap
        return None

    async def capability_for_tenant(
        self, tenant_id: uuid.UUID, provider: str | None = None
    ) -> SubscriptionCapability | None:
        """Proveedor de suscripción del tenant (ADR-0106).

        - `provider` explícito: debe estar registrado Y habilitado para el
          tenant; si no, error claro (configuración visible en el panel).
        - Sin `provider`: 0 habilitados → None; 1 → ese; 2+ →
          `SubscriptionProviderAmbiguity`.
        """
        from core.plugin_manager import plugin_manager
        from core.shared.tenant import tenant_scope

        async with tenant_scope(tenant_id):
            habilitados = [
                pid for pid in self._caps if plugin_manager.is_active(pid)
            ]
        if provider is not None:
            cap = self._caps.get(provider)
            if cap is None:
                raise SubscriptionError(
                    f"El proveedor de suscripción '{provider}' no está instalado.",
                    code="proveedor_no_registrado",
                )
            if provider not in habilitados:
                raise SubscriptionError(
                    f"El proveedor de suscripción '{provider}' no está habilitado para este negocio.",
                    code="proveedor_deshabilitado",
                )
            return cap
        if len(habilitados) == 0:
            return None
        if len(habilitados) == 1:
            return self._caps[habilitados[0]]
        raise SubscriptionProviderAmbiguity(habilitados)


subscription_registry = SubscriptionRegistry()
