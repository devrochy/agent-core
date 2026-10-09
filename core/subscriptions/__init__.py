"""Capacidad de suscripciones del core — contrato único v1 (HU-SUB-01, ADR-0106).

Un proveedor (`pack_subscriptions` hoy; `pack_cobranza` con HU-SUB-15) registra
una `SubscriptionCapability` con las 8 operaciones del contrato. El core
despacha las tools `sub_*` al proveedor del negocio y commerce consume el
registro para activar la suscripción al pagarse un pedido.

Selección del proveedor: `capability_for_tenant` resuelve entre los proveedores
habilitados para el tenant (packs de su AgentSpec); dos habilitados sin elección
explícita lanzan `SubscriptionProviderAmbiguity` (configuración visible en el panel).
La elección del negocio vive en `tenants.settings["subscriptions"]["provider"]`
(HU-SUB-09): `capability_del_negocio` la aplica y `estado_proveedor` la explica.
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
    # HU-SUB-07 (ADR-0111)
    referencia: str | None  # pedido que la creó
    cambio_programado: dict[str, Any] | None  # downgrade al próximo período
    cambio: str  # resultado de change_plan: inmediato | pendiente_pago | programado
    cargo_cambio: dict[str, Any] | None  # cargo de la diferencia (monto, link, referencia)


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

    async def enabled_for_tenant(self, tenant_id: uuid.UUID) -> list[str]:
        """Proveedores registrados que el negocio tiene habilitados en su AgentSpec.

        Antes se miraba `plugin_manager.is_active`, que es por PROCESO: con todos los
        packs activos en el worker, cualquier proveedor contaba para cualquier negocio
        (HU-SUB-09).
        """
        from core.agents.spec import enabled_packs, load_active_spec
        from core.shared.tenant import tenant_scope

        async with tenant_scope(tenant_id):
            spec = await load_active_spec(tenant_id)
        packs = enabled_packs(spec)
        return sorted(pid for pid in self._caps if pid in packs)

    async def capability_for_tenant(
        self, tenant_id: uuid.UUID, provider: str | None = None
    ) -> SubscriptionCapability | None:
        """Proveedor de suscripción del tenant (ADR-0106).

        - `provider` explícito: debe estar registrado Y habilitado para el
          tenant; si no, error claro (configuración visible en el panel).
        - Sin `provider`: 0 habilitados → None; 1 → ese; 2+ →
          `SubscriptionProviderAmbiguity`.
        """
        habilitados = await self.enabled_for_tenant(tenant_id)
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


# ------------------------------------------------- elección del negocio (HU-SUB-09)
SETTINGS_KEY = "subscriptions"


async def proveedor_configurado(tenant_id: uuid.UUID) -> str | None:
    """Proveedor elegido por el negocio (`tenants.settings.subscriptions.provider`).

    `tenants` es tabla de sistema (sin RLS): el `tenant_id` debe venir del principal o
    del contexto (`get_current_tenant_id()`), nunca del cuerpo de una petición ni del LLM.
    """
    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return None
    valor = ((tenant.settings or {}).get(SETTINGS_KEY) or {}).get("provider")
    return str(valor) if valor else None


async def guardar_proveedor(tenant_id: uuid.UUID, provider: str | None) -> None:
    """Guarda (o borra con None) la elección. Quien llama valida que esté habilitado.

    Igual que `proveedor_configurado`: el `tenant_id` sale del principal, nunca del body.
    """
    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise SubscriptionError("Negocio no encontrado.", code="sin_negocio")
        settings = dict(tenant.settings or {})
        bloque = dict(settings.get(SETTINGS_KEY) or {})
        if provider:
            bloque["provider"] = provider
        else:
            bloque.pop("provider", None)
        settings[SETTINGS_KEY] = bloque
        tenant.settings = settings


async def capability_del_negocio(tenant_id: uuid.UUID) -> SubscriptionCapability | None:
    """Proveedor que usa el negocio: el elegido o, sin elección, el único habilitado.

    Lanza `SubscriptionProviderAmbiguity` con 2+ habilitados sin elección y
    `SubscriptionError` si el elegido ya no está habilitado.
    """
    return await subscription_registry.capability_for_tenant(
        tenant_id, await proveedor_configurado(tenant_id)
    )


class EstadoProveedor(TypedDict):
    estado: str  # ok | sin_proveedor | ambiguo | invalido
    configurado: str | None
    efectivo: str | None
    habilitados: list[str]
    mensaje: str


async def estado_proveedor(tenant_id: uuid.UUID) -> EstadoProveedor:
    """Diagnóstico para el panel: qué proveedor se usa y, si no hay uno claro, por qué."""
    habilitados = await subscription_registry.enabled_for_tenant(tenant_id)
    configurado = await proveedor_configurado(tenant_id)
    base: EstadoProveedor = {
        "estado": "ok", "configurado": configurado, "efectivo": None, "habilitados": habilitados, "mensaje": "",
    }
    try:
        cap = await subscription_registry.capability_for_tenant(tenant_id, configurado)
    except SubscriptionProviderAmbiguity as exc:
        return {**base, "estado": "ambiguo", "mensaje": exc.message}
    except SubscriptionError as exc:
        return {**base, "estado": "invalido", "mensaje": exc.message}
    if cap is None:
        return {
            **base,
            "estado": "sin_proveedor",
            "mensaje": "No hay una habilidad de suscripciones activa: activa Suscripciones "
            "(cobro en línea) o Cobranza (cobro en campo) para vender planes recurrentes.",
        }
    return {**base, "efectivo": cap.provider_id}


# ------------------------------------------------- catálogo de planes (HU-SUB-07, ADR-0111)
@runtime_checkable
class PlanCatalog(Protocol):
    """Catálogo de planes de un pack (commerce): el precio de un plan nunca sale del LLM."""

    provider_id: str

    async def plan_por_codigo(self, codigo: str) -> tuple[str, PlanSnapshot] | None:
        """(variant_id, snapshot) del plan por SKU o `variant_id`, o None si no existe."""
        ...


@dataclass
class PlanCatalogRegistry:
    _catalogos: dict[str, PlanCatalog] = field(default_factory=dict)

    def register(self, provider_id: str, catalogo: PlanCatalog) -> None:
        self._catalogos[provider_id] = catalogo

    def unregister(self, provider_id: str) -> None:
        self._catalogos.pop(provider_id, None)

    async def para_negocio(self, tenant_id: uuid.UUID) -> PlanCatalog | None:
        """Catálogo de un pack habilitado en el AgentSpec del negocio (el primero)."""
        from core.agents.spec import enabled_packs, load_active_spec
        from core.shared.tenant import tenant_scope

        async with tenant_scope(tenant_id):
            spec = await load_active_spec(tenant_id)
        packs = enabled_packs(spec)
        for pid in sorted(self._catalogos):
            if pid in packs:
                return self._catalogos[pid]
        return None


plan_catalog_registry = PlanCatalogRegistry()


async def permite_varias(capability: SubscriptionCapability) -> bool:
    """¿El proveedor permite varias suscripciones por cliente? Operación opcional del
    contrato (ADR-0111): si no la implementa, no las permite."""
    metodo = getattr(capability, "permite_varias", None)
    if metodo is None:
        return False
    try:
        return bool(await metodo())
    except Exception:
        log.warning("permite_varias_fallo", provider=getattr(capability, "provider_id", "?"), exc_info=True)
        return False


# ------------------------------------------------- operaciones opcionales (HU-SH-04, ADR-0123)
# El cobro de la plataforma por dos canales necesita dos operaciones más del proveedor. Son
# opcionales (como `permite_varias`): un proveedor que no las implementa devuelve None y el
# llamador lo trata como «sin soporte» (aviso por el panel; la conciliación lo muestra).


async def registrar_pago_externo(
    capability: SubscriptionCapability,
    subscription_id: uuid.UUID,
    *,
    inicio: Any,
    fin: Any,
    referencia: str,
    canal: str,
) -> dict[str, Any] | None:
    """El período que empieza en `inicio` lo pagó otro canal: el cargo queda pagado y el
    proveedor no lo vuelve a cobrar. `{"estado": registrado | ya_registrado | duplicado, …}`."""
    metodo = getattr(capability, "registrar_pago_externo", None)
    if metodo is None:
        return None
    return dict(await metodo(subscription_id, inicio=inicio, fin=fin, referencia=referencia, canal=canal))


async def cobrar_periodo(
    capability: SubscriptionCapability, subscription_id: uuid.UUID, *, inicio: Any
) -> dict[str, Any] | None:
    """Genera ya el cargo del período que empieza en `inicio`, con su link y su aviso al
    cliente. `{"estado": generado | ya_existia | no_cobrable | desalineado, …}`."""
    metodo = getattr(capability, "cobrar_periodo", None)
    if metodo is None:
        return None
    return dict(await metodo(subscription_id, inicio=inicio))
