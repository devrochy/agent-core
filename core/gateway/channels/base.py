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
from typing import Any, Literal

from core.shared.exceptions import ChannelError

# Categoría de un error del proveedor (ADR-0186): `decide_retry` decide por ella, no por el
# código de cada proveedor. POLICY es interna: el rechazo anti-baneo de waweb, que se reprograma.
ErrorCategory = Literal[
    "TRANSIENT",
    "RATE_LIMIT",
    "QUOTA",
    "AMBIGUOUS",
    "AUTH",
    "CONFIG",
    "WINDOW",
    "OPT_OUT",
    "RECIPIENT",
    "CONTENT",
    "PERMANENT",
    "POLICY",
]


def category_for_http_status(http_status: int | None) -> ErrorCategory:
    """Categoría de un error que el adaptador no supo clasificar: un 429 es rate limit, otro 4xx
    es permanente (reintentar el mismo pedido no lo arregla) y un 5xx o la falta de respuesta es
    transitorio."""
    if http_status == 429:
        return "RATE_LIMIT"
    if http_status is not None and 400 <= http_status < 500:
        return "PERMANENT"
    return "TRANSIENT"


class ProviderError(ChannelError):
    """Error de un proveedor de canal (Meta, Twilio), ya clasificado por categoría.

    `code` es el código propio del proveedor (131047 de Meta, 63016 de Twilio) y `retry_after`
    los segundos que pidió esperar, si los dio. Sin `category`, sale de `http_status`."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        code: int | None = None,
        http_status: int | None = None,
        category: ErrorCategory | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.provider = provider
        self.code = code
        self.http_status = http_status
        self.category: ErrorCategory = category or category_for_http_status(http_status)
        self.retry_after = retry_after
        super().__init__(message)

    @property
    def message(self) -> str:
        return str(self)


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
    # Id del botón o de la fila de lista que tocó el cliente (`plan:premium`). `content` sigue
    # siendo el título visible; el id solo lo usa el motor de procedimientos (HU-AOS-05).
    interactive_id: str = ""


class InboundAdapter(ABC):
    provider: str = ""
    channel: str = ""

    @abstractmethod
    def verify_request(
        self, raw_body: bytes, headers: dict[str, str], secret: str, *, url: str | None = None
    ) -> bool:
        """Verifica la firma del webhook. NUNCA procesar sin verificar. `url` es la dirección
        exacta a la que llegó el webhook, para los proveedores que la firman (Twilio)."""

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

    async def send_template(
        self,
        credentials: dict[str, str],
        to_external_id: str,
        name: str,
        language: str,
        components: list[dict[str, Any]],
    ) -> str:
        """Envía una plantilla aprobada (HU-ARCH-13, ADR-0108). Solo WhatsApp Cloud
        tiene plantillas: los demás canales no tienen ventana de 24 h y no la usan."""
        raise ChannelError(f"El canal {self.provider or '?'} no envía plantillas")

    async def send_content_template(
        self,
        credentials: dict[str, str],
        to_external_id: str,
        content_sid: str,
        variables: dict[str, str],
    ) -> str:
        """Envía una plantilla por su id de contenido (`HX…` de Twilio, ADR-0186) con sus
        variables numeradas (`{"1": "Ana"}`). Solo la implementan los proveedores con plantillas
        por id de contenido."""
        raise ChannelError(f"El canal {self.provider or '?'} no envía plantillas por id de contenido")

    async def send_interactive(
        self, credentials: dict[str, str], to_external_id: str, interactive: dict[str, Any]
    ) -> str:
        """Envía un mensaje interactivo con botones de respuesta (HU-AOS-09) o con lista
        (HU-INMO-03). `interactive` es `{"type": "button", "body": str, "buttons": [{"id", "title"}]}`
        o `{"type": "list", "body": str, "button": str, "rows": [{"id", "title", "description"?}]}`,
        ya saneado por el kernel. Solo WhatsApp Cloud lo implementa: el sender decide la capacidad del canal antes de
        encolar, así que los demás canales reciben la confirmación como texto y nunca llegan aquí."""
        raise ChannelError(f"El canal {self.provider or '?'} no envía mensajes interactivos")
