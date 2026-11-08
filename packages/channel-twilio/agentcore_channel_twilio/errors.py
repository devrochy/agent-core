"""Error de la API de Twilio → `ProviderError` con categoría (ADR-0186).

`decide_retry` decide por la categoría, no por el código. Los títulos de cada código están en
https://www.twilio.com/docs/api/errors/<código> (leídos el 2026-11-07). Un código que no está aquí
cae por su estado HTTP: 401 es AUTH, 429 RATE_LIMIT, otro 4xx PERMANENT y 5xx TRANSIENT.

Lo mismo sirve para el `ErrorCode` de un status callback (`category_for_code`): ahí no hay
reintento, pero la categoría dice si hay que registrar una baja (OPT_OUT) o degradar la conexión.
"""

from __future__ import annotations

import httpx

from core.gateway.channels.base import ErrorCategory, ProviderError, category_for_http_status

CODE_CATEGORIES: dict[int, ErrorCategory] = {
    # Credenciales incorrectas, caducadas o de otra cuenta.
    20003: "AUTH",
    # Concurrencia de la REST API (HTTP 429) y tasa del canal o del sender.
    20429: "RATE_LIMIT",
    63018: "RATE_LIMIT",
    # Límite de mensajes en 24 h de la cuenta.
    63038: "QUOTA",
    # Mensaje libre fuera de la ventana de 24 h: hay que usar una plantilla.
    63016: "WINDOW",
    # El destinatario respondió STOP.
    21610: "OPT_OUT",
    # Destinatario: número inválido, sin WhatsApp, inválido para Meta, o en un experimento de Meta.
    21211: "RECIPIENT",
    21614: "RECIPIENT",
    63003: "RECIPIENT",
    63024: "RECIPIENT",
    63032: "RECIPIENT",
    30003: "RECIPIENT",
    30005: "RECIPIENT",
    30006: "RECIPIENT",
    # Configuración de la cuenta o del sender: remitente que no es de la cuenta, sender o WABA
    # bloqueados o deshabilitados por Meta, cuenta trial, región sin permiso.
    21606: "CONFIG",
    21608: "CONFIG",
    21408: "CONFIG",
    63007: "CONFIG",
    63051: "CONFIG",
    63112: "CONFIG",
    # Contenido: rechazado por el canal o violación de su política, filtrado.
    63005: "CONTENT",
    63013: "CONTENT",
    30007: "CONTENT",
    # Meta decidió no entregar un mensaje de marketing. No es una baja del cliente: no se registra
    # como OPT_OUT, solo no se reintenta.
    63049: "PERMANENT",
    30004: "PERMANENT",
    30008: "PERMANENT",
    20404: "PERMANENT",
}

_MAX_MESSAGE = 300


def category_for_code(code: int | None, http_status: int | None = None) -> ErrorCategory:
    if code is not None and code in CODE_CATEGORIES:
        return CODE_CATEGORIES[code]
    if http_status == 401:
        return "AUTH"
    return category_for_http_status(http_status)


def _retry_after(response: httpx.Response) -> float | None:
    # Twilio no documenta `Retry-After`; si algún día lo manda, se respeta.
    raw = response.headers.get("retry-after")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


def error_from_response(response: httpx.Response) -> ProviderError:
    """Cuerpo de error de Twilio: `{"code", "message", "more_info", "status"}` (`code` puede
    faltar). El mensaje se recorta: puede traer el número del destinatario."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_code = body.get("code")
    code = raw_code if isinstance(raw_code, int) else None
    message = str(body.get("message") or response.reason_phrase or "Error de Twilio")[:_MAX_MESSAGE]
    return ProviderError(
        f"Twilio {code or response.status_code}: {message}",
        provider="twilio",
        code=code,
        http_status=response.status_code,
        category=category_for_code(code, response.status_code),
        retry_after=_retry_after(response),
    )
