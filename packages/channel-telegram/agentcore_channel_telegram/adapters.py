"""Adaptadores del canal `telegram` (ADR-0199 §4-7): entrada y salida.

`TelegramInbound` verifica el secreto del webhook y convierte un `Update` de la Bot API en `InboundMessage`. El
webhook del core le pasa el update JUNTO con el `bot_id` de la conexión que ya validó, porque los ids de Telegram
no son globales: `update_id` es del bot y `message_id` es del chat. Así los ids que viajan al core son únicos entre
bots y entre negocios:

- mensaje entrante: `tg:<bot_id>:in:<chat_id>:<message_id>`; toque de botón: `tg:<bot_id>:cb:<callback_query.id>`
  (`external_message_id`, la llave de idempotencia). No se usa `update_id`: tras una semana sin updates Telegram lo
  elige al azar y podría repetir uno viejo, que la idempotencia descartaría en silencio;
- cliente: el id del usuario de Telegram (`customer_external_id`), que en un chat privado es también el `chat_id`.

Solo chats privados con una persona. Lo demás (grupos, canales, mensajes editados, stickers, documentos, ubicaciones,
contactos de otra persona) se ignora con una lista vacía: el webhook responde 200 y Telegram no reintenta.

Lo que acepta: texto (`/start` llega como texto), foto y nota de voz (con su caption), el toque de un botón inline
(`callback_query`) y el contacto que el cliente comparte de sí mismo (`contact.user_id` igual al remitente), que llega
como `shared_phone` en E.164. Los medios van en `media` con el `file_id` de Telegram; los descarga la ingesta del core.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from typing import Any

import httpx

from agentcore_channel_telegram.client import TelegramClient
from agentcore_channel_telegram.limites import (
    ARCHIVO_DESCARGA_MAX,
    CAPTION_MAX,
    TEXTO_MAX,
    cabe_callback_data,
)
from core.gateway.channels.base import (
    InboundAdapter,
    InboundMessage,
    OutboundAdapter,
    ProviderError,
)

PROVIDER = "telegram"
CHANNEL = "telegram"
HEADER_SECRETO = "x-telegram-bot-api-secret-token"
# Tope de lo que se toma de un texto o caption: Telegram ya lo limita a 4096 y 1024.
_MAX_TEXTO = 4096
_MAX_INTERACTIVE_ID = 256


def id_entrante(bot_id: str, chat_id: int | str, message_id: int) -> str:
    return f"tg:{bot_id}:in:{chat_id}:{message_id}"


def id_toque(bot_id: str, callback_query_id: str) -> str:
    return f"tg:{bot_id}:cb:{callback_query_id}"


def id_saliente(bot_id: str, chat_id: int | str, message_id: int) -> str:
    return f"tg:{bot_id}:out:{chat_id}:{message_id}"


def _entero(valor: object) -> int | None:
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else None


def _texto(valor: object, tope: int = _MAX_TEXTO) -> str:
    return valor[:tope] if isinstance(valor, str) else ""


def _persona(remitente: object) -> Mapping[str, Any] | None:
    """El remitente si es una persona (no un bot) con id."""
    if not isinstance(remitente, Mapping) or remitente.get("is_bot") is True:
        return None
    return remitente if _entero(remitente.get("id")) is not None else None


def _nombre(persona: Mapping[str, Any]) -> str | None:
    partes = [_texto(persona.get("first_name"), 64), _texto(persona.get("last_name"), 64)]
    nombre = " ".join(p for p in partes if p).strip()
    return nombre or None


def _chat_privado(mensaje: object, persona: Mapping[str, Any]) -> bool:
    """Chat privado con ESA persona: en un chat privado, `chat.id` es el id del usuario."""
    if not isinstance(mensaje, Mapping):
        return False
    chat = mensaje.get("chat")
    return isinstance(chat, Mapping) and chat.get("type") == "private" and chat.get("id") == persona.get("id")


def _foto(mensaje: Mapping[str, Any]) -> dict[str, str] | None:
    """La foto más grande que el bot puede descargar (Telegram manda varias resoluciones)."""
    tamanos = mensaje.get("photo")
    if not isinstance(tamanos, list):
        return None
    validas = [
        t
        for t in tamanos
        if isinstance(t, Mapping)
        and isinstance(t.get("file_id"), str)
        and (_entero(t.get("file_size")) or 0) <= ARCHIVO_DESCARGA_MAX
    ]
    if not validas:
        return None
    mayor = max(validas, key=lambda t: (_entero(t.get("width")) or 0) * (_entero(t.get("height")) or 0))
    return {"type": "image/jpeg", "id": str(mayor["file_id"])}


_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def telefono_propio(mensaje: Mapping[str, Any], persona: Mapping[str, Any]) -> str | None:
    """El teléfono del contacto compartido, en E.164, solo si es del propio remitente: un contacto reenviado (de otra
    persona) o sin `user_id` no cuenta. Telegram a veces lo manda sin `+`."""
    contacto = mensaje.get("contact")
    if not isinstance(contacto, Mapping) or contacto.get("user_id") != persona.get("id"):
        return None
    digitos = "".join(c for c in _texto(contacto.get("phone_number"), 32) if c.isdigit())
    numero = f"+{digitos}"
    return numero if _E164.match(numero) else None


def _voz(mensaje: Mapping[str, Any]) -> dict[str, str] | None:
    voz = mensaje.get("voice")
    if not isinstance(voz, Mapping) or not isinstance(voz.get("file_id"), str):
        return None
    tipo = _texto(voz.get("mime_type"), 64) or "audio/ogg"
    return {"type": tipo if tipo.startswith("audio/") else "audio/ogg", "id": str(voz["file_id"])}


def _titulo_del_boton(mensaje: object, data: str) -> str:
    """El texto visible del botón tocado, buscado en el teclado del mensaje: `callback_query` solo trae su `data`."""
    if not isinstance(mensaje, Mapping):
        return ""
    teclado = (mensaje.get("reply_markup") or {}).get("inline_keyboard") if isinstance(
        mensaje.get("reply_markup"), Mapping
    ) else None
    if not isinstance(teclado, list):
        return ""
    for fila in teclado:
        if not isinstance(fila, list):
            continue
        for boton in fila:
            if isinstance(boton, Mapping) and boton.get("callback_data") == data:
                return _texto(boton.get("text"), 256)
    return ""


class TelegramInbound(InboundAdapter):
    provider = PROVIDER
    channel = CHANNEL

    def verify_request(
        self, raw_body: bytes, headers: dict[str, str], secret: str, *, url: str | None = None
    ) -> bool:
        """El encabezado `X-Telegram-Bot-Api-Secret-Token` tiene que ser el secreto de la conexión. Sin secreto
        guardado falla cerrado. Comparación en tiempo constante."""
        if not secret:
            return False
        recibido = next((v for k, v in headers.items() if k.lower() == HEADER_SECRETO), "")
        return bool(recibido) and hmac.compare_digest(recibido.encode(), secret.encode())

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundMessage]:
        """`payload` es `{"bot_id": "<id del bot de la conexión>", "update": <Update>}`."""
        bot_id = str(payload.get("bot_id") or "")
        return self.parse_update(payload.get("update"), bot_id=bot_id)

    def parse_update(self, update: object, *, bot_id: str) -> list[InboundMessage]:
        if not bot_id or not isinstance(update, Mapping):
            return []
        update_id = _entero(update.get("update_id"))
        if update_id is None:
            return []
        if isinstance(update.get("message"), Mapping):
            mensaje = self._mensaje(update["message"], bot_id)
        elif isinstance(update.get("callback_query"), Mapping):
            mensaje = self._toque(update["callback_query"], bot_id)
        else:
            # edited_message, channel_post, my_chat_member…: no se procesan.
            mensaje = None
        return [mensaje] if mensaje is not None else []

    def _mensaje(self, mensaje: Mapping[str, Any], bot_id: str) -> InboundMessage | None:
        persona = _persona(mensaje.get("from"))
        message_id = _entero(mensaje.get("message_id"))
        if persona is None or message_id is None or not _chat_privado(mensaje, persona):
            return None
        media = [m for m in (_foto(mensaje), _voz(mensaje)) if m is not None]
        telefono = telefono_propio(mensaje, persona)
        # El número compartido es también lo que el cliente «dijo»: así el turno no llega vacío al agente.
        contenido = _texto(mensaje.get("text")) or _texto(mensaje.get("caption"), 1024) or (telefono or "")
        if not contenido and not media:
            return None  # sticker, documento, ubicación, contacto de otra persona…
        return InboundMessage(
            provider=PROVIDER,
            channel=CHANNEL,
            external_message_id=id_entrante(bot_id, persona["id"], message_id),
            customer_external_id=str(persona["id"]),
            customer_name=_nombre(persona),
            content=contenido,
            connection_external_id=bot_id,
            media=media,
            shared_phone=telefono,
        )

    def _toque(self, toque: Mapping[str, Any], bot_id: str) -> InboundMessage | None:
        persona = _persona(toque.get("from"))
        data = _texto(toque.get("data"), _MAX_INTERACTIVE_ID)
        toque_id = _texto(toque.get("id"), 64)
        if persona is None or not data or not toque_id or not _chat_privado(toque.get("message"), persona):
            return None
        return InboundMessage(
            provider=PROVIDER,
            channel=CHANNEL,
            external_message_id=id_toque(bot_id, toque_id),
            customer_external_id=str(persona["id"]),
            customer_name=_nombre(persona),
            # Lo que el cliente ve en el botón; el id va aparte para el motor de procedimientos (HU-AOS-05).
            content=_titulo_del_boton(toque.get("message"), data) or data,
            connection_external_id=bot_id,
            interactive_id=data,
        )


def callback_query_id(update: object) -> str:
    """El id del toque a contestar con `answerCallbackQuery` (quita el reloj del botón), o `""`."""
    if not isinstance(update, Mapping) or not isinstance(update.get("callback_query"), Mapping):
        return ""
    valor = update["callback_query"].get("id")
    return valor if isinstance(valor, str) else ""


# --------------------------------------------------------------------------- salida (PR core 4)
def _chat_id(to_external_id: str) -> int:
    """El cliente de Telegram es el id de su usuario, que en un chat privado es también el `chat_id`."""
    try:
        return int(str(to_external_id).strip())
    except ValueError:
        raise ProviderError("Destinatario de Telegram inválido", provider=PROVIDER, category="RECIPIENT") from None


def _partes(texto: str) -> list[str]:
    """El texto en trozos de hasta 4096 caracteres (el core ya parte en burbujas; esto solo evita un 400)."""
    texto = texto or ""
    if len(texto) <= TEXTO_MAX:
        return [texto]
    return [texto[i : i + TEXTO_MAX] for i in range(0, len(texto), TEXTO_MAX)]


def teclado_inline(interactive: Mapping[str, Any]) -> dict[str, Any]:
    """Botones o lista del kernel → teclado inline, una fila por opción. La lista NO se degrada aquí (adenda §9 del
    ADR-0186): si hay que degradar, el sender lo decide antes de encolar. Los ids ya llegan cortos (≤ 64 bytes): el
    core cambia los largos por un token antes de llamar al adaptador. Un id que no cabe es un error de contenido."""
    if interactive.get("type") == "list":
        opciones = [(f.get("title"), f.get("id")) for f in interactive.get("rows") or [] if isinstance(f, Mapping)]
    else:
        opciones = [(b.get("title"), b.get("id")) for b in interactive.get("buttons") or [] if isinstance(b, Mapping)]
    filas = []
    for titulo, data in opciones:
        if not isinstance(titulo, str) or not titulo or not isinstance(data, str) or not cabe_callback_data(data):
            raise ProviderError(
                "Opción no enviable por Telegram (título vacío o id de más de 64 bytes)",
                provider=PROVIDER,
                category="CONTENT",
            )
        filas.append([{"text": titulo, "callback_data": data}])
    if not filas:
        raise ProviderError("Mensaje interactivo sin opciones", provider=PROVIDER, category="CONTENT")
    return {"inline_keyboard": filas}


class TelegramOutbound(OutboundAdapter):
    """Salida por la Bot API. Credenciales: `bot_token` y `bot_id` (las arma `resolve_credentials`). Devuelve el id
    global `tg:<bot_id>:out:<chat_id>:<message_id>`: el `message_id` de Telegram es por chat y la llave de `messages`
    no lleva proveedor."""

    provider = PROVIDER

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _cliente(self, credentials: Mapping[str, str]) -> TelegramClient:
        return TelegramClient.from_credentials(credentials, transport=self._transport)

    @staticmethod
    def _id(credentials: Mapping[str, str], chat_id: int, enviado: object) -> str:
        message_id = enviado.get("message_id") if isinstance(enviado, Mapping) else None
        if not isinstance(message_id, int):
            # Telegram respondió ok sin `message_id`: no se sabe qué quedó; se trata como ambiguo.
            raise ProviderError("Telegram no devolvió el id del mensaje", provider=PROVIDER, category="AMBIGUOUS")
        return id_saliente(str(credentials.get("bot_id") or ""), chat_id, message_id)

    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        chat_id = _chat_id(to_external_id)
        cliente = self._cliente(credentials)
        enviado: object = None
        for parte in _partes(text):
            enviado = await cliente.send_message(chat_id, parte)
        return self._id(credentials, chat_id, enviado)

    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str:
        chat_id = _chat_id(to_external_id)
        enviado = await self._cliente(credentials).send_photo(chat_id, image_url, caption=caption[:CAPTION_MAX])
        return self._id(credentials, chat_id, enviado)

    async def send_audio(
        self, credentials: dict[str, str], to_external_id: str, audio_url: str, caption: str = ""
    ) -> str:
        """Nota de voz (`sendVoice`) por URL: el TTS ya la deja en OGG/Opus. Si Telegram la rechaza, el error es
        `CONTENT` y el envío del core se degrada al texto."""
        chat_id = _chat_id(to_external_id)
        enviado = await self._cliente(credentials).send_voice(chat_id, audio_url, caption=caption[:CAPTION_MAX])
        return self._id(credentials, chat_id, enviado)

    async def send_interactive(
        self, credentials: dict[str, str], to_external_id: str, interactive: dict[str, Any]
    ) -> str:
        chat_id = _chat_id(to_external_id)
        cuerpo = _texto(interactive.get("body"))
        if not cuerpo:
            raise ProviderError("Mensaje interactivo sin cuerpo", provider=PROVIDER, category="CONTENT")
        enviado = await self._cliente(credentials).send_message(
            chat_id, cuerpo, reply_markup=teclado_inline(interactive)
        )
        return self._id(credentials, chat_id, enviado)
