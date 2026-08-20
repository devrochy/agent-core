"""Contrato de adaptadores de canal — indexado por PROVEEDOR (ADR-029 de agent-sale).

Un canal (whatsapp) puede tener varios proveedores (meta, twilio). El registry
resuelve el adapter por proveedor; la conexión concreta vive en la tabla
channel_connections con credenciales cifradas.

Para agregar Telegram/Instagram: implementar InboundAdapter + OutboundAdapter
y registrarlos en registry.py. El orchestrator no cambia.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundMessage:
    provider: str           # "meta"
    channel: str            # "whatsapp"
    external_message_id: str
    customer_external_id: str   # "whatsapp:+573001234567"
    customer_name: str | None
    content: str
    connection_external_id: str  # clave de ruteo (phone_number_id)
    # Medios: el core los tolera sin romper; mod-media-processor los transforma
    media: list[dict[str, str]] = field(default_factory=list)
    # [{"type": "audio/ogg", "id": "...", "url": "..."}]
    media_context: str = ""  # texto generado por el procesador de medios


class InboundAdapter(ABC):
    provider: str = ""
    channel: str = ""

    @abstractmethod
    def verify_request(self, raw_body: bytes, headers: dict[str, str], secret: str) -> bool:
        """Verifica la firma del webhook. NUNCA procesar sin verificar."""

    @abstractmethod
    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundMessage]:
        """Devuelve los mensajes del payload. Lista vacía es NORMAL
        (callbacks de estado de entrega, echoes, etc.)."""


class OutboundAdapter(ABC):
    provider: str = ""

    @abstractmethod
    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        """Envía un texto. Devuelve el message_id del proveedor."""

    @abstractmethod
    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str: ...
