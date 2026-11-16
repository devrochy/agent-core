"""Error de la Bot API de Telegram → `ProviderError` con categoría (ADR-0199).

La Bot API responde `{"ok": false, "error_code": <int>, "description": "...", "parameters": {...}}`. La documentación
dice que el contenido de `error_code` puede cambiar, así que se clasifica por el estado HTTP y, dentro de un 400 o un
403, por la `description`:

- 401: token revocado o inválido → `AUTH` (el envío pasa la conexión a `revoked`).
- 403: el cliente bloqueó el bot, desactivó su cuenta o nunca le escribió → `RECIPIENT`. **Nunca `OPT_OUT`**: un
  bloqueo de Telegram es reversible y no es una baja de marketing.
- 400 de destinatario («chat not found», «user not found», «PEER_ID_INVALID»…) → `RECIPIENT`; otro 400 → `CONTENT`
  (sin `parse_mode` en v1 casi no hay; reintentar el mismo pedido no lo arregla).
- 404: método o archivo inexistente → `CONFIG`. NO es `AUTH`: un `file_path` vencido no debe revocar una conexión
  sana. El token mal formado (que también da 404) lo traduce el alta con `getMe`, no este mapeo.
- 409: el bot tiene otro webhook o alguien llama `getUpdates` → `CONFIG`.
- 429: `RATE_LIMIT` con `parameters.retry_after`.
- 5xx: `TRANSIENT`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from core.gateway.channels.base import ErrorCategory, ProviderError, category_for_http_status

_MAX_MENSAJE = 300
# El token del bot (`<bot_id>:<secreto>`) nunca va en un mensaje de error, aunque la Bot API no lo devuelve.
_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")

# Frases de la `description` que dicen que el problema es el destinatario, no el contenido ni el bot.
_FRASES_DESTINATARIO = (
    "bot was blocked by the user",
    "user is deactivated",
    "bot can't initiate conversation",
    "bot was kicked",
    "chat not found",
    "user not found",
    "peer_id_invalid",
    "bot can't send messages to bots",
)


def _descripcion(body: Mapping[str, Any]) -> str:
    raw = body.get("description")
    texto = raw if isinstance(raw, str) else ""
    return _TOKEN.sub("[redactado]", texto)[:_MAX_MENSAJE]


def _retry_after(body: Mapping[str, Any]) -> float | None:
    parametros = body.get("parameters")
    if not isinstance(parametros, Mapping):
        return None
    valor = parametros.get("retry_after")
    if isinstance(valor, bool) or not isinstance(valor, int | float) or valor < 0:
        return None
    return float(valor)


def categoria(http_status: int | None, descripcion: str) -> ErrorCategory:
    """Categoría de un error de la Bot API por estado HTTP y `description`."""
    texto = descripcion.lower()
    if http_status == 401:
        return "AUTH"
    if http_status == 403:
        return "RECIPIENT"
    if http_status == 400:
        if any(frase in texto for frase in _FRASES_DESTINATARIO):
            return "RECIPIENT"
        return "CONTENT"
    if http_status in (404, 409):
        return "CONFIG"
    if http_status == 429:
        return "RATE_LIMIT"
    return category_for_http_status(http_status)


def error_from_response(http_status: int, body: object) -> ProviderError:
    """`ProviderError` de una respuesta con `ok=false` (o de un estado no 2xx). Sin el token ni datos del cliente:
    la `description` de Telegram no los trae y igual se recorta."""
    cuerpo: Mapping[str, Any] = body if isinstance(body, Mapping) else {}
    descripcion = _descripcion(cuerpo)
    raw_code = cuerpo.get("error_code")
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    return ProviderError(
        f"Telegram {code or http_status}: {descripcion or 'error de la Bot API'}",
        provider="telegram",
        code=code,
        http_status=http_status,
        category=categoria(http_status, descripcion),
        retry_after=_retry_after(cuerpo),
    )
