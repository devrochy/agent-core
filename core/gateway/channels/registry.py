"""Registro de adaptadores por proveedor."""

from __future__ import annotations

from core.gateway.channels.base import InboundAdapter, OutboundAdapter
from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound
from core.gateway.channels.whatsapp.outbound import WhatsAppMetaOutbound

_INBOUND: dict[str, InboundAdapter] = {
    "meta": WhatsAppMetaInbound(),
}
_OUTBOUND: dict[str, OutboundAdapter] = {
    "meta": WhatsAppMetaOutbound(),
}


def inbound_adapter_for(provider: str) -> InboundAdapter:
    adapter = _INBOUND.get(provider)
    if adapter is None:
        raise KeyError(f"Proveedor de entrada desconocido: {provider}")
    return adapter


def outbound_adapter_for(provider: str) -> OutboundAdapter:
    adapter = _OUTBOUND.get(provider)
    if adapter is None:
        raise KeyError(f"Proveedor de salida desconocido: {provider}")
    return adapter
