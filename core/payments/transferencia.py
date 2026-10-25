"""Datos de transferencia del negocio como capacidad del core (HU-SUB-24, adenda de ADR-0067).

El negocio escribe sus cuentas una sola vez (hoy en la configuración de `pack_commerce`). Un pack
las **registra** como `TransferDetailsProvider`; otro (p. ej. `pack_subscriptions`) las **consulta**
sin importarlo (invariante nº5), con el mismo molde que `payment_registry`: el core filtra por pack
activo del negocio.

El bloque de texto lo arma `formatear_cuentas` y nunca el LLM: las cuentas son datos del negocio
que se imprimen tal cual, no instrucciones (invariante nº7). Comercio y suscripciones usan la misma
función para que el cliente vea el mismo bloque en el chat de ventas y en el aviso de cobro.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TransferDetailsProvider(Protocol):
    plugin_id: str

    async def cuentas_de_transferencia(self) -> list[dict[str, Any]]:
        """Cuentas del negocio activo: `entity`, `accountType`, `accountNumber`, `holderName`,
        `holderDocument`. Vacía si el negocio no configuró ninguna."""
        ...


class TransferDetailsRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, TransferDetailsProvider] = {}

    def register(self, plugin_id: str, provider: TransferDetailsProvider) -> None:
        """Idempotente: registrar dos veces el mismo pack reemplaza la entrada."""
        self._providers[plugin_id] = provider

    def unregister(self, plugin_id: str) -> None:
        self._providers.pop(plugin_id, None)

    def provider_for(self, enabled: set[str]) -> TransferDetailsProvider | None:
        """Primer proveedor de un pack habilitado. `enabled` es obligatorio: sin los packs del
        negocio activo no se ofrece ningún proveedor."""
        for pid, provider in self._providers.items():
            if pid in enabled:
                return provider
        return None


transfer_registry = TransferDetailsRegistry()


def formatear_cuentas(cuentas: list[dict[str, Any]] | None) -> str:
    """Bloque «Datos para tu transferencia» o `""` si no hay cuentas. Una línea por cuenta."""
    if not cuentas:
        return ""
    lineas = ["💳 Datos para tu transferencia:"]
    for cuenta in cuentas:
        linea = f"• {cuenta.get('entity', '')}"
        if cuenta.get("accountType"):
            linea += f" ({cuenta['accountType']})"
        if cuenta.get("accountNumber"):
            linea += f" N° {cuenta['accountNumber']}"
        if cuenta.get("holderName"):
            linea += f" a nombre de {cuenta['holderName']}"
        if cuenta.get("holderDocument"):
            linea += f" CC {cuenta['holderDocument']}"
        lineas.append(linea)
    return "\n".join(lineas)


async def datos_de_transferencia(enabled: set[str]) -> str:
    """Bloque de transferencia del negocio activo, o `""` si ningún pack habilitado lo ofrece o el
    negocio no tiene cuentas. Un proveedor que falla no rompe el aviso: se trata como sin datos."""
    provider = transfer_registry.provider_for(enabled)
    if provider is None:
        return ""
    try:
        return formatear_cuentas(await provider.cuentas_de_transferencia())
    except Exception:
        from core.shared.logger import get_logger

        get_logger(__name__).warning("transferencia_proveedor_fallo", exc_info=True)
        return ""
