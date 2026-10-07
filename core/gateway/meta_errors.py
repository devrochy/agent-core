"""Clasificación de errores de Graph API y política de reintento.

Un solo lugar que decide "¿reintento esto, y con qué backoff?" para
cualquier caller del cliente único (`core/gateway/meta_graph.py`). Antes
esta lógica estaba duplicada 3 veces dentro de `meta_preflight.py`.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import httpx

from core.shared.exceptions import AgentCoreError
from core.shared.logger import get_logger

log = get_logger(__name__)

ErrorCategory = Literal["RATE_LIMIT", "AUTH", "TRANSIENT", "PERMANENT", "POLICY"]

# Meta: códigos de rate limit / uso excesivo de la app -- no reintentar en
# bucle, se resuelve con backoff (real, no un "reintentá ahora" ciego --
# CLAUDE.md invariante nº7: un rate limit devuelto por un tercero no se
# ejecuta como instrucción de negocio, se trata como una señal a respetar).
# 130429 (throughput de mensajería excedido) se agrupa acá a propósito:
# misma semántica de "esperá y reintentá", nunca se descarta.
RATE_LIMIT_CODES = {4, 17, 32, 613, 80001, 80004, 130429}
AUTH_CODE = 190

# 131047: re-engagement -- envío libre (no plantilla) fuera de la ventana de
# 24h de WhatsApp. Semántica OBSERVADA en incidentes reales, NO confirmada
# contra doc primaria de 2026 -- se trata como PERMANENT (nunca reintentar,
# reintentar no cambia el resultado) con un mensaje que sugiere el único
# camino real: reenviar como plantilla.
REENGAGEMENT_WINDOW_CODE = 131047

# Códigos/subcódigos vistos en la práctica pero NO documentados oficialmente
# por Meta a la fecha (2026) -- se logean para juntar datos reales, nunca se
# les inventa un significado. 80007 en particular apareció en incidentes
# reales sin explicación oficial.
_UNDOCUMENTED_CODES = {80007}

_DEFAULT_BACKOFF_MS = 60_000  # 60s, arranque del backoff exponencial


class GraphError(AgentCoreError):
    """Error de una llamada a Graph API, ya clasificado."""

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory,
        http_status: int | None = None,
        code: int | None = None,
        error_subcode: int | None = None,
        error_type: str | None = None,
        fbtrace_id: str | None = None,
        error_user_title: str | None = None,
        error_user_msg: str | None = None,
        is_transient: bool = False,
        retry_after_ms: int | None = None,
    ) -> None:
        self.category = category
        self.http_status = http_status
        self.code = code
        self.error_subcode = error_subcode
        self.error_type = error_type
        self.fbtrace_id = fbtrace_id
        self.error_user_title = error_user_title
        self.error_user_msg = error_user_msg
        self.is_transient = is_transient
        self.retry_after_ms = retry_after_ms
        super().__init__(message)

    @property
    def message(self) -> str:
        return str(self)

    @property
    def user_text(self) -> str:
        """El motivo que da Meta para mostrarle a una persona (`error_user_title` y
        `error_user_msg`), o `message` si no vino. "Invalid parameter" sola no dice qué
        parámetro ni por qué; el motivo sí (p. ej. que el número ya está en otra cuenta)."""
        parts = [p.strip() for p in (self.error_user_title, self.error_user_msg) if p and p.strip()]
        return ": ".join(parts) if parts else self.message


def _retry_after_from_usage(headers: httpx.Headers, attempt: int) -> int:
    """`estimated_time_to_regain_access` (minutos, en X-Business-Use-Case-Usage)
    -> ms. Si no viene, backoff exponencial desde 60s."""
    raw = headers.get("x-business-use-case-usage")
    if raw:
        try:
            usage = json.loads(raw)
            for entries in usage.values():
                for entry in entries:
                    minutes = entry.get("estimated_time_to_regain_access")
                    if minutes is not None:
                        return int(minutes) * 60_000
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    return _DEFAULT_BACKOFF_MS * (2**attempt)


def classify_and_build_error(
    *,
    http_status: int,
    body: dict[str, Any] | None,
    headers: httpx.Headers,
    raw_text: str = "",
    attempt: int = 0,
) -> GraphError:
    """Arma un `GraphError` ya clasificado a partir de la respuesta de Meta.

    `body` es `None` si la respuesta no era JSON (HTML de error, típico en
    un 429) -- en ese caso no hay `error.code` que clasificar, se trata como
    PERMANENT con el texto crudo truncado."""
    if body is None:
        return GraphError(
            f"Respuesta no-JSON de Meta (HTTP {http_status}): {raw_text[:500]}",
            category="PERMANENT",
            http_status=http_status,
        )

    error = body.get("error") or {}
    code = error.get("code")
    subcode = error.get("error_subcode")
    message = str(error.get("message") or "Error de Meta sin mensaje")
    error_type = error.get("type")
    fbtrace_id = error.get("fbtrace_id")
    error_user_title = error.get("error_user_title")
    error_user_msg = error.get("error_user_msg")
    is_transient = bool(error.get("is_transient"))

    if code in _UNDOCUMENTED_CODES or (code == AUTH_CODE and subcode is not None):
        log.warning(
            "graph_error_codigo_no_documentado",
            code=code,
            error_subcode=subcode,
            fbtrace_id=fbtrace_id,
        )

    if code == REENGAGEMENT_WINDOW_CODE:
        return GraphError(
            f"Fuera de la ventana de reenganche de 24h (code {code}): {message}. "
            "Reenviá como plantilla -- reintentar el mismo envío libre no va a funcionar.",
            category="PERMANENT",
            http_status=http_status,
            code=code,
            error_subcode=subcode,
            error_type=error_type,
            fbtrace_id=fbtrace_id,
            error_user_title=error_user_title,
            error_user_msg=error_user_msg,
            is_transient=False,
            retry_after_ms=None,
        )

    if code == AUTH_CODE:
        return GraphError(
            f"Meta rechazó las credenciales (OAuthException): {message}. "
            "Requiere reautorización del usuario -- no reintentar.",
            category="AUTH",
            http_status=http_status,
            code=code,
            error_subcode=subcode,
            error_type=error_type,
            fbtrace_id=fbtrace_id,
            error_user_title=error_user_title,
            error_user_msg=error_user_msg,
            is_transient=False,
            retry_after_ms=None,
        )

    if code in RATE_LIMIT_CODES:
        return GraphError(
            f"Rate limit de Meta (code {code}): {message}",
            category="RATE_LIMIT",
            http_status=http_status,
            code=code,
            error_subcode=subcode,
            error_type=error_type,
            fbtrace_id=fbtrace_id,
            error_user_title=error_user_title,
            error_user_msg=error_user_msg,
            is_transient=True,
            retry_after_ms=_retry_after_from_usage(headers, attempt),
        )

    if is_transient:
        return GraphError(
            f"Error transitorio de Meta: {message}",
            category="TRANSIENT",
            http_status=http_status,
            code=code,
            error_subcode=subcode,
            error_type=error_type,
            fbtrace_id=fbtrace_id,
            error_user_title=error_user_title,
            error_user_msg=error_user_msg,
            is_transient=True,
            retry_after_ms=_DEFAULT_BACKOFF_MS * (2**attempt),
        )

    return GraphError(
        f"Error de Meta: {message}",
        category="PERMANENT",
        http_status=http_status,
        code=code,
        error_subcode=subcode,
        error_type=error_type,
        fbtrace_id=fbtrace_id,
        error_user_title=error_user_title,
        error_user_msg=error_user_msg,
        is_transient=False,
        retry_after_ms=None,
    )
