"""Registro de adaptadores por proveedor (entrada) y por (proveedor, canal) (salida).

La entrada sigue indexada solo por `provider`: una única clase
(`WhatsAppMetaInbound`) ya sabe parsear WhatsApp/Messenger/Instagram --
comparten el mismo formato de payload de webhook de Meta lo suficiente
como para no justificar clases separadas. La SALIDA sí necesita
implementaciones distintas por canal (WhatsApp Cloud API vs Messenger Send
API tienen endpoints y reglas de negocio incompatibles -- ventana de
servicio, message tags), así que `_OUTBOUND` se indexa por
`(provider, channel)`. Antes de este cambio, WhatsApp y Messenger
compartían la clave `"meta"` y era estructuralmente imposible registrar un
adapter de Messenger sin pisar el de WhatsApp (deuda documentada en
`whatsapp/inbound.py` desde que Messenger se agregó solo de entrada)."""

from __future__ import annotations

from core.config import get_settings
from core.gateway.channels.base import InboundAdapter, OutboundAdapter
from core.gateway.channels.instagram.outbound import InstagramOutbound
from core.gateway.channels.messenger.outbound import FacebookMessengerOutbound
from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound
from core.gateway.channels.whatsapp.outbound import WhatsAppMetaOutbound
from core.shared.logger import get_logger

log = get_logger(__name__)

_INBOUND: dict[str, InboundAdapter] = {
    "meta": WhatsAppMetaInbound(),
}
_OUTBOUND: dict[tuple[str, str], OutboundAdapter] = {
    ("meta", "whatsapp"): WhatsAppMetaOutbound(),
    ("meta", "messenger"): FacebookMessengerOutbound(),
    ("meta", "instagram"): InstagramOutbound(),
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


def outbound_adapter_for(provider: str, channel: str = "whatsapp") -> OutboundAdapter:
    """`channel` default "whatsapp" por retrocompatibilidad con callers
    viejos que todavía no pasan canal explícito -- todo caller nuevo debe
    pasarlo (`conversation.channel`, ya disponible en `sender.py`)."""
    settings = get_settings()
    if settings.demo_mode:
        if settings.app_env != "development":
            raise ValueError("DEMO_MODE solo está permitido en APP_ENV=development")
        log.warning("outbound_noop", note="DEMO_MODE: envío de canal no-op (dev)")
        return NoopOutboundAdapter()
    adapter = _OUTBOUND.get((provider, channel))
    if adapter is None:
        raise KeyError(f"Proveedor/canal de salida desconocido: {provider}/{channel}")
    return adapter
