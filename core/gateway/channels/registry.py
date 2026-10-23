"""Registro de adaptadores por proveedor.

Los adaptadores built-in (`meta`) se registran acá; los de canales externos se
descubren por **entry_points** en el grupo `agent_core.channels` (ADR-0052): un
paquete de canal expone una clase con `.inbound()` / `.outbound()`. Sumar un canal
nuevo ya no toca el core.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

from core.config import get_settings
from core.gateway.channels.base import InboundAdapter, OutboundAdapter
from core.gateway.channels.email import EmailSmtpOutbound
from core.gateway.channels.messenger.outbound import MessengerMetaOutbound
from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound
from core.gateway.channels.whatsapp.outbound import WhatsAppMetaOutbound
from core.shared.logger import get_logger

log = get_logger(__name__)

ENTRY_POINT_GROUP = "agent_core.channels"

_INBOUND: dict[str, InboundAdapter] = {
    "meta": WhatsAppMetaInbound(),
}
_OUTBOUND: dict[str, OutboundAdapter] = {
    "meta": WhatsAppMetaOutbound(),
    "smtp": EmailSmtpOutbound(),  # correo saliente (ADR-0071)
}

# Adaptadores de salida por (proveedor, canal): Messenger e Instagram comparten el proveedor `meta`
# con WhatsApp y se distinguen por canal (ADR-0152). Sin entrada para el canal, rige el de proveedor.
_OUTBOUND_BY_CHANNEL: dict[tuple[str, str], OutboundAdapter] = {
    ("meta", "messenger"): MessengerMetaOutbound(),
}

# Canales de mensajería de Meta que NO usan el adaptador de WhatsApp aunque compartan proveedor.
CANALES_DE_MENSAJERIA_META = frozenset({"messenger", "instagram"})

_scanned = False


def _register(provider: str, inbound: Any, outbound: Any) -> None:
    if not provider:
        return
    if isinstance(inbound, InboundAdapter):
        _INBOUND.setdefault(provider, inbound)
    if isinstance(outbound, OutboundAdapter):
        _OUTBOUND.setdefault(provider, outbound)


def scan() -> None:
    """Escanea entry_points `agent_core.channels` y registra sus adaptadores.

    Cada entry_point puede apuntar a:
    - una clase con métodos de clase `inbound()` / `outbound()`, o
    - un módulo con atributos `PROVIDER`, `InboundAdapter`, `OutboundAdapter`.

    Idempotente; los built-in (`meta`) no se pisan (`setdefault`).
    """
    global _scanned
    if _scanned:
        return
    _scanned = True
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
            provider = getattr(obj, "provider", None) or getattr(obj, "PROVIDER", None)
            inbound = None
            outbound = None
            if callable(getattr(obj, "inbound", None)):
                inbound = obj.inbound()
            elif getattr(obj, "InboundAdapter", None) is not None:
                inbound = obj.InboundAdapter()
            if callable(getattr(obj, "outbound", None)):
                outbound = obj.outbound()
            elif getattr(obj, "OutboundAdapter", None) is not None:
                outbound = obj.OutboundAdapter()
            if not provider and inbound is not None:
                provider = getattr(inbound, "provider", None)
            _register(str(provider or ""), inbound, outbound)
            log.info("channel_detected", provider=provider, entry_point=ep.name)
        except Exception:
            log.exception("channel_load_failed", entry_point=ep.name)


def providers() -> list[str]:
    """Proveedores de canal registrados (built-in + entry_points)."""
    scan()
    return sorted(set(_INBOUND) | set(_OUTBOUND))


class NoopOutboundAdapter(OutboundAdapter):
    """Devuelve éxito sin tocar el canal. Solo para DEMO_MODE (dev)."""

    provider = "noop"

    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        return "demo"

    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str:
        return "demo"


def inbound_adapter_for(provider: str) -> InboundAdapter:
    scan()
    adapter = _INBOUND.get(provider)
    if adapter is None:
        raise KeyError(f"Proveedor de entrada desconocido: {provider}")
    return adapter


def outbound_adapter_for(provider: str, channel: str | None = None) -> OutboundAdapter:
    """El adaptador de salida del proveedor; con `channel`, el específico de ese canal si existe
    (`("meta", "messenger")`) y, si no, el del proveedor (WhatsApp para `meta`)."""
    scan()
    settings = get_settings()
    if settings.demo_mode:
        if settings.app_env != "development":
            raise ValueError("DEMO_MODE solo está permitido en APP_ENV=development")
        log.warning("outbound_noop", note="DEMO_MODE: envío de canal no-op (dev)")
        return NoopOutboundAdapter()
    adapter = _OUTBOUND_BY_CHANNEL.get((provider, channel or ""))
    if adapter is None and channel in CANALES_DE_MENSAJERIA_META:
        # Sin adaptador propio (Instagram, hasta HU-CHAN-04) NO se cae al de WhatsApp: mandaría un
        # identificador de cliente de otro canal como si fuera un teléfono.
        raise KeyError(f"Sin adaptador de salida para {provider}/{channel}")
    adapter = adapter or _OUTBOUND.get(provider)
    if adapter is None:
        raise KeyError(f"Proveedor de salida desconocido: {provider}")
    return adapter
