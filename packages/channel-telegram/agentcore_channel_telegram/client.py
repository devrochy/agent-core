"""Cliente HTTP de la Bot API de Telegram (ADR-0199).

Solo contra `https://api.telegram.org`. El token del bot va EN LA RUTA (`/bot<token>/método`), así que:

- se valida su forma antes de armar una URL (un valor guardado nunca altera la ruta);
- ningún log, mensaje de error ni excepción encadenada lo lleva: los errores de `httpx` (cuyo `request.url` lo trae) se
  relanzan `from None` y solo se registra su tipo. Las trazas de OTel y Sentry lo tapan aparte
  (`core/observability/tracing.py` y `sentry_setup.py`);
- `mascara()` es lo único que se puede mostrar: `…:ABCD`.

Cada error sale como `ProviderError` con su categoría (`errors.py`). Un corte de red a mitad de un envío
(`send*`) queda `AMBIGUOUS`: Telegram pudo haberlo entregado. En una lectura o en una operación que se puede repetir
sin efecto (`getMe`, `setWebhook`, `getFile`…), `TRANSIENT`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from agentcore_channel_telegram.errors import error_from_response
from agentcore_channel_telegram.limites import (
    ALLOWED_UPDATES,
    ARCHIVO_DESCARGA_MAX,
    secreto_webhook_valido,
)
from core.gateway.channels.base import ProviderError
from core.shared.logger import get_logger

log = get_logger(__name__)

API_HOST = "api.telegram.org"
API_BASE = f"https://{API_HOST}"
TIMEOUT = httpx.Timeout(10.0, connect=3.0)

# `<bot_id>:<secreto>`. La documentación no fija el largo del secreto; se exige uno razonable.
_TOKEN = re.compile(r"^\d{1,20}:[A-Za-z0-9_-]{20,128}$")
_TOKEN_EN_TEXTO = re.compile(r"\d+:[A-Za-z0-9_-]{20,}")
# `file_path` que devuelve `getFile` (`photos/file_1.jpg`, `voice/file_2.oga`): relativo, sin `..` ni query.
_FILE_PATH = re.compile(r"^[A-Za-z0-9_./-]{1,256}$")


def token_valido(token: object) -> bool:
    return isinstance(token, str) and bool(_TOKEN.match(token))


def mascara(token: object) -> str:
    """Lo único del token que se puede mostrar o registrar: los últimos 4 caracteres (`…:ABCD`)."""
    if not isinstance(token, str) or len(token) < 8:
        return "…"
    return f"…:{token[-4:]}"


def redactar(texto: str) -> str:
    """Tapa cualquier token de bot en un texto (`/bot<token>/` incluido)."""
    return _TOKEN_EN_TEXTO.sub("[redactado]", texto)


def _error_de_config(mensaje: str) -> ProviderError:
    return ProviderError(mensaje, provider="telegram", category="CONFIG")


class TelegramClient:
    """Un bot de Telegram. Sin estado: se crea por operación."""

    def __init__(self, bot_token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not token_valido(bot_token):
            raise _error_de_config("Token de bot de Telegram inválido")
        self._token = bot_token
        self._transport = transport

    @classmethod
    def from_credentials(
        cls, credentials: Mapping[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
    ) -> TelegramClient:
        return cls(str(credentials.get("bot_token") or ""), transport=transport)

    def __repr__(self) -> str:  # nunca el token, ni en un traceback con locales
        return f"TelegramClient({mascara(self._token)})"

    # ------------------------------------------------------------------ alta y salud
    async def get_me(self) -> dict[str, Any]:
        return await self._llamar("getMe", idempotente=True)

    async def set_webhook(
        self,
        url: str,
        secret_token: str,
        *,
        allowed_updates: Sequence[str] = ALLOWED_UPDATES,
        drop_pending_updates: bool = False,
    ) -> bool:
        if not url.startswith("https://"):
            raise _error_de_config("La URL del webhook de Telegram debe ser https")
        if not secreto_webhook_valido(secret_token):
            raise _error_de_config("Secreto de webhook de Telegram inválido")
        resultado = await self._llamar(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": list(allowed_updates),
                "drop_pending_updates": drop_pending_updates,
            },
            idempotente=True,
        )
        return resultado is True

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> bool:
        resultado = await self._llamar(
            "deleteWebhook", {"drop_pending_updates": drop_pending_updates}, idempotente=True
        )
        return resultado is True

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self._llamar("getWebhookInfo", idempotente=True)

    # ------------------------------------------------------------------ salida
    async def send_message(
        self, chat_id: int | str, text: str, *, reply_markup: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        return await self._llamar("sendMessage", payload, idempotente=False)

    async def send_photo(
        self, chat_id: int | str, photo: str | bytes, *, caption: str = "", filename: str = "foto.jpg"
    ) -> dict[str, Any]:
        return await self._enviar_archivo("sendPhoto", "photo", chat_id, photo, caption, filename, "image/jpeg")

    async def send_voice(
        self, chat_id: int | str, voice: str | bytes, *, caption: str = "", filename: str = "voz.ogg"
    ) -> dict[str, Any]:
        return await self._enviar_archivo("sendVoice", "voice", chat_id, voice, caption, filename, "audio/ogg")

    async def answer_callback_query(self, callback_query_id: str, *, text: str = "") -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        # Contestar dos veces no hace daño (la segunda falla por vieja): se trata como repetible.
        return await self._llamar("answerCallbackQuery", payload, idempotente=True) is True

    async def edit_message_reply_markup(
        self, chat_id: int | str, message_id: int, *, reply_markup: Mapping[str, Any] | None = None
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        return await self._llamar("editMessageReplyMarkup", payload, idempotente=True)

    # ------------------------------------------------------------------ medios entrantes
    async def get_file(self, file_id: str) -> dict[str, Any]:
        return await self._llamar("getFile", {"file_id": file_id}, idempotente=True)

    async def download_file(self, file_path: str, *, max_bytes: int = ARCHIVO_DESCARGA_MAX) -> bytes:
        """Descarga un archivo por su `file_path` (de `getFile`), cortando en `max_bytes`: un archivo más grande es
        `CONTENT` (no se reintenta)."""
        if not _FILE_PATH.match(file_path) or ".." in file_path or file_path.startswith("/"):
            raise _error_de_config("file_path de Telegram inválido")
        url = f"{API_BASE}/file/bot{self._token}/{file_path}"
        started = time.monotonic()
        try:
            async with (
                httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport, follow_redirects=False) as client,
                client.stream("GET", url) as response,
            ):
                if not response.is_success:
                    await response.aread()
                    raise error_from_response(response.status_code, _json(response))
                partes: list[bytes] = []
                total = 0
                async for trozo in response.aiter_bytes():
                    total += len(trozo)
                    if total > max_bytes:
                        raise ProviderError(
                            "El archivo de Telegram supera el tamaño máximo",
                            provider="telegram",
                            category="CONTENT",
                        )
                    partes.append(trozo)
        except httpx.TransportError as exc:
            log.warning("telegram_api_inalcanzable", op="downloadFile", error=type(exc).__name__)
            raise ProviderError(
                "No se pudo descargar el archivo de Telegram", provider="telegram", category="TRANSIENT"
            ) from None
        log.info(
            "telegram_api_call",
            op="downloadFile",
            bytes=total,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return b"".join(partes)

    # ------------------------------------------------------------------ transporte
    async def _enviar_archivo(
        self,
        metodo: str,
        campo: str,
        chat_id: int | str,
        archivo: str | bytes,
        caption: str,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        if isinstance(archivo, bytes):
            data: dict[str, Any] = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption
            return await self._llamar(
                metodo,
                idempotente=False,
                data=data,
                files={campo: (filename, archivo, content_type)},
            )
        payload: dict[str, Any] = {"chat_id": chat_id, campo: archivo}
        if caption:
            payload["caption"] = caption
        return await self._llamar(metodo, payload, idempotente=False)

    async def _llamar(
        self,
        metodo: str,
        payload: Mapping[str, Any] | None = None,
        *,
        idempotente: bool,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
    ) -> Any:
        url = f"{API_BASE}/bot{self._token}/{metodo}"
        kwargs: dict[str, Any] = {"data": data, "files": files} if files is not None else {"json": dict(payload or {})}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT, transport=self._transport, follow_redirects=False
            ) as client:
                response = await client.post(url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            log.warning("telegram_api_inalcanzable", op=metodo, error=type(exc).__name__)
            raise ProviderError(
                "No se pudo conectar con Telegram", provider="telegram", category="TRANSIENT"
            ) from None
        except httpx.TransportError as exc:
            # Timeout de lectura o corte a mitad de respuesta: en un envío Telegram pudo haberlo entregado.
            categoria = "TRANSIENT" if idempotente else "AMBIGUOUS"
            log.warning("telegram_api_ambiguo", op=metodo, error=type(exc).__name__, category=categoria)
            raise ProviderError(
                "Telegram no respondió a tiempo", provider="telegram", category=categoria
            ) from None

        duration_ms = round((time.monotonic() - started) * 1000)
        body = _json(response)
        if response.is_success and isinstance(body, dict) and body.get("ok") is True:
            log.info("telegram_api_call", op=metodo, status=response.status_code, duration_ms=duration_ms)
            return body.get("result")
        if response.is_success:
            # 2xx sin `ok=true`: en un envío no se sabe si Telegram lo hizo. Clasificado, nunca un error suelto.
            categoria = "TRANSIENT" if idempotente else "AMBIGUOUS"
            log.warning(
                "telegram_api_cuerpo_invalido", op=metodo, status=response.status_code, category=categoria
            )
            raise ProviderError(
                "Telegram respondió sin un cuerpo válido", provider="telegram", category=categoria
            )
        error = error_from_response(response.status_code, body)
        log.warning(
            "telegram_api_error",
            op=metodo,
            status=response.status_code,
            code=error.code,
            category=error.category,
            duration_ms=duration_ms,
        )
        raise error


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None
