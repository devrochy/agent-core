"""Clasificación de los errores de Twilio en las categorías de `decide_retry` (ADR-0186).

Cada código y su categoría salen del diccionario oficial (`https://www.twilio.com/docs/api/errors/<código>`,
leído el 2026-11-07); `tests/test_channel_twilio_errores.py` fija la tabla fila por fila. Un
código que no está en la tabla se clasifica por el estado HTTP (4xx permanente, 5xx
transitorio) y se loguea: nunca se le inventa un significado.

El cuerpo de error de la REST es `{"code", "message", "more_info", "status"}`; `code` y
`more_info` pueden faltar. La documentación no menciona `Retry-After`: si llegara, se respeta;
si no, `decide_retry` espera su plazo por defecto.
"""

from __future__ import annotations

import httpx

from core.gateway.channels.base import ErrorCategory, ProviderError, category_for_http_status
from core.shared.logger import get_logger

log = get_logger(__name__)

PROVIDER = "twilio"

CODE_CATEGORIES: dict[int, ErrorCategory] = {
    # Credenciales o cuenta: la conexión pasa a `revoked` (PR-5).
    20003: "AUTH",  # Permission Denied (también cuenta suspendida o cerrada)
    30002: "AUTH",  # Account suspended
    # Límites: se reintenta respetando el plazo.
    20429: "RATE_LIMIT",  # Too many requests
    63018: "RATE_LIMIT",  # Rate limit exceeded for Channel
    63038: "QUOTA",  # Account exceeded the daily messages limit (ventana de 24 h)
    30001: "TRANSIENT",  # Queue overflow
    30008: "TRANSIENT",  # Unknown error
    # Configuración del sender o de la WABA: la conexión pasa a `degraded` (PR-5).
    63007: "CONFIG",  # Twilio could not find a Channel with the specified 'From' address
    63051: "CONFIG",  # WhatsApp Sender or Account is Locked
    63112: "CONFIG",  # Meta disabled the WhatsApp Business Account connected to this Sender
    63020: "CONFIG",  # Twilio encountered a Business Manager account error
    21408: "CONFIG",  # Message blocked: permissions disabled for the destination region
    # Ventana de 24 h: hay que usar una plantilla.
    63016: "WINDOW",  # Outside messaging window. For WhatsApp, use a Message Template instead
    # El cliente se dio de baja.
    21610: "OPT_OUT",  # Attempt to send to unsubscribed recipient
    # Destinatario: corregir el número o esperar a que el cliente actúe.
    21211: "RECIPIENT",  # Invalid 'To' Phone Number
    21612: "RECIPIENT",  # Message cannot be sent with the current combination of "To" and/or "From"
    21614: "RECIPIENT",  # 'To' number is not a valid mobile number
    63003: "RECIPIENT",  # Channel could not find To address
    63024: "RECIPIENT",  # Invalid message recipient
    63032: "RECIPIENT",  # We cannot send this message to this user because of a WhatsApp limitation
    63049: "RECIPIENT",  # Meta chose not to deliver this WhatsApp marketing message
    30003: "RECIPIENT",  # Unreachable destination handset
    30004: "RECIPIENT",  # Message blocked
    30005: "RECIPIENT",  # Unknown destination handset
    30006: "RECIPIENT",  # Landline or unreachable carrier
    # Contenido o plantilla.
    63005: "CONTENT",  # Channel rejected content
    63013: "CONTENT",  # Channel policy violation
    63021: "CONTENT",  # Channel invalid content error
    63027: "CONTENT",  # Template does not exist for a language and locale
    63028: "CONTENT",  # Number of parameters provided does not match the expected number
    63030: "CONTENT",  # Unsupported parameter for type of channels message
    30007: "CONTENT",  # Message filtered
}

# Pistas para quien lee el error en el panel, solo donde hay un camino claro.
_HINTS: dict[ErrorCategory, str] = {
    "WINDOW": "Fuera de la ventana de 24 h: envía una plantilla aprobada.",
    "AUTH": "Twilio rechazó las credenciales de la cuenta: revisa el Account SID y el Auth Token.",
    "CONFIG": "El número de WhatsApp en Twilio no está listo para enviar: revisa el sender.",
    "OPT_OUT": "El cliente se dio de baja de los mensajes de este número.",
}


def category_for_code(code: int | None, http_status: int | None = None) -> ErrorCategory:
    """Categoría de un código de Twilio; sin entrada en la tabla, la del estado HTTP. Sirve
    también para el `ErrorCode` de un status callback, que no trae estado HTTP."""
    if code is not None and code in CODE_CATEGORIES:
        return CODE_CATEGORIES[code]
    if http_status in (401, 403):
        return "AUTH"
    if code is not None:
        log.warning("twilio_error_codigo_no_mapeado", code=code, http_status=http_status)
    return category_for_http_status(http_status)


def build_error(
    message: str, *, code: int | None, http_status: int | None, retry_after: float | None = None
) -> ProviderError:
    category = category_for_code(code, http_status)
    hint = _HINTS.get(category)
    text = f"Error de Twilio{f' {code}' if code else ''}: {message}"
    if hint:
        text = f"{text}. {hint}"
    return ProviderError(
        text,
        provider=PROVIDER,
        code=code,
        http_status=http_status,
        category=category,
        retry_after=retry_after,
    )


def _retry_after(headers: httpx.Headers) -> float | None:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None  # la forma de fecha HTTP no se usa en Twilio; mejor el plazo por defecto
    return seconds if seconds >= 0 else None


def error_from_response(response: httpx.Response) -> ProviderError:
    """Arma el error de una respuesta 4xx/5xx (o de un 2xx que no era JSON)."""
    code: int | None = None
    message = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        raw_code = body.get("code")
        try:
            code = int(raw_code) if raw_code is not None else None
        except (TypeError, ValueError):
            code = None
        message = str(body.get("message") or "")
    else:
        # El cuerpo crudo (HTML de un proxy) va solo al log, recortado.
        log.warning(
            "twilio_respuesta_no_json", http_status=response.status_code, body=response.text[:300]
        )
    status = response.status_code if response.status_code >= 400 else None
    return build_error(
        message or f"respuesta HTTP {response.status_code}",
        code=code,
        http_status=status,
        retry_after=_retry_after(response.headers),
    )


_SAFE_METHODS = frozenset({"GET", "HEAD", "DELETE"})


def error_from_transport(exc: httpx.TransportError, *, method: str = "POST") -> ProviderError:
    """Fallo de red. Si la conexión no llegó a abrirse, nada salió: TRANSIENT. Si el pedido
    pudo haber llegado (timeout de lectura o escritura, corte a mitad de respuesta) y no es
    idempotente, AMBIGUOUS: Twilio no ofrece un header de idempotencia al crear mensajes."""
    name = type(exc).__name__
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        category: ErrorCategory = "TRANSIENT"
    elif method.upper() in _SAFE_METHODS:
        category = "TRANSIENT"
    else:
        category = "AMBIGUOUS"
    return ProviderError(f"Error de red con Twilio ({name})", provider=PROVIDER, category=category)
