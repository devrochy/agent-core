"""Registro de adaptadores por proveedor."""

from __future__ import annotations

from core.config import get_settings
from core.gateway.channels.base import InboundAdapter, OutboundAdapter
from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound
from core.gateway.channels.whatsapp.outbound import WhatsAppMetaOutbound
from core.shared.logger import get_logger

log = get_logger(__name__)

_INBOUND: dict[str, InboundAdapter] = {
    "meta": WhatsAppMetaInbound(),
}
_OUTBOUND: dict[str, OutboundAdapter] = {
    "meta": WhatsAppMetaOutbound(),
}


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
    adapter = _INBOUND.get(provider)
    if adapter is None:
        raise KeyError(f"Proveedor de entrada desconocido: {provider}")
    return adapter


def outbound_adapter_for(provider: str) -> OutboundAdapter:
    settings = get_settings()
    if settings.demo_mode:
        if settings.app_env != "development":
            raise ValueError("DEMO_MODE solo está permitido en APP_ENV=development")
        log.warning("outbound_noop", note="DEMO_MODE: envío de canal no-op (dev)")
        return NoopOutboundAdapter()
    adapter = _OUTBOUND.get(provider)
    if adapter is None:
        raise KeyError(f"Proveedor de salida desconocido: {provider}")
    return adapter
