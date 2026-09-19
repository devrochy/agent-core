"""Cliente único de Graph API para el panel (`verify_meta_credentials`,
`meta_preflight.py`). Envío de mensajes (`outbound.py`) y el CLI
(`cli/commands/meta.py`) quedan fuera de esta tanda a propósito -- ver
docs/adr y el plan de esta feature.

Centraliza lo que antes estaba duplicado en cada call-site: `appsecret_proof`,
medición de latencia + registro en `graph_api_calls`, parseo de uso
(`core/gateway/meta_usage.py`) y clasificación de errores
(`core/gateway/meta_errors.py`).
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any, Literal

import httpx

from core.config import get_settings
from core.db.models import GraphApiCall
from core.db.session import get_session
from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.gateway.meta_usage import record_usage
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
_TIMEOUT_S = 30.0


def appsecret_proof(token: str, app_secret: str) -> str:
    """HMAC-SHA256 sobre el TOKEN (nunca sobre el app_id) -- prueba que quien
    llama conoce el App Secret, exigido por Meta para llamadas server-to-server."""
    return hmac.new(app_secret.encode(), token.encode(), hashlib.sha256).hexdigest()


async def graph_fetch(
    path: str,
    *,
    token: str,
    app_secret: str,
    method: Literal["GET", "POST", "DELETE"] = "GET",
    body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    version: str | None = None,
    tenant_id: uuid.UUID | None = None,
    connection_id: uuid.UUID | None = None,
) -> Any:
    """Llama a Graph API. `path` empieza con "/" (ej. "/123456789/subscriptions").

    - El token SIEMPRE va en `Authorization: Bearer` -- nunca en query.
    - `appsecret_proof` SIEMPRE viaja como query param.
    - La versión SIEMPRE sale de config (`core/config.py`, `META_GRAPH_VERSION`),
      nunca hardcodeada acá -- `version` es un override opcional para callers
      que ya resolvieron su propia `MetaConnection.graph_api_version`.
    - Registra la llamada en `graph_api_calls` y el uso en `graph_api_usage`
      (vía `record_usage`) en TODA respuesta, éxito o error.
    """
    resolved_version = version or get_settings().graph_api_version
    resolved_tenant_id = tenant_id if tenant_id is not None else get_current_tenant_id()
    url = f"{GRAPH_BASE}/{resolved_version}{path}"

    params = dict(query or {})
    params["appsecret_proof"] = appsecret_proof(token, app_secret)
    headers = {"Authorization": f"Bearer {token}"}

    started = time.monotonic()
    error: GraphError | None = None
    status_code: int | None = None
    response: httpx.Response | None = None

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.request(
                method, url, params=params, json=body, headers=headers,
                # cache no aplica en httpx (no cachea por default, a diferencia
                # de fetch() en Next.js -- no hay nada que desactivar acá).
            )
    except httpx.TimeoutException:
        error = GraphError(
            f"Timeout ({_TIMEOUT_S}s) llamando a Graph API: {path}",
            category="TRANSIENT",
            is_transient=True,
            retry_after_ms=5_000,
        )
    except httpx.ConnectError as exc:
        error = GraphError(
            f"No se pudo conectar a Graph API: {exc}", category="TRANSIENT", is_transient=True, retry_after_ms=5_000
        )

    duration_ms = int((time.monotonic() - started) * 1000)

    if response is not None:
        status_code = response.status_code
        if status_code >= 400:
            try:
                parsed_body = response.json()
            except Exception:
                parsed_body = None
            error = classify_and_build_error(
                http_status=status_code, body=parsed_body, headers=response.headers, raw_text=response.text
            )
        await record_usage(
            resolved_tenant_id, connection_id, response.headers, is_error=error is not None, path=path
        )

    await _log_call(
        tenant_id=resolved_tenant_id,
        connection_id=connection_id,
        method=method,
        path=path,
        status_code=status_code,
        duration_ms=duration_ms,
        error=error,
        request_id=response.headers.get("x-fb-request-id") if response is not None else None,
    )

    if error is not None:
        raise error

    assert response is not None  # si no hubo error de red, siempre hay response acá
    try:
        return response.json()
    except Exception:
        return None


async def _log_call(
    *,
    tenant_id: uuid.UUID | None,
    connection_id: uuid.UUID | None,
    method: str,
    path: str,
    status_code: int | None,
    duration_ms: int,
    error: GraphError | None,
    request_id: str | None = None,
) -> None:
    """Inserta en `graph_api_calls`. Nunca rompe la llamada real: si falta
    `tenant_id` (no debería pasar -- los callers de panel siempre están en un
    `tenant_scope` activo) loguea y no inserta, en vez de fallar la request."""
    if tenant_id is None:
        log.warning("graph_api_call_sin_tenant_context", path=path)
        return
    async with get_session() as session:
        session.add(
            GraphApiCall(
                tenant_id=tenant_id,
                connection_id=connection_id,
                method=method,
                endpoint=path,
                status_code=status_code,
                latency_ms=duration_ms,
                error_code=error.code if error else None,
                error_subcode=error.error_subcode if error else None,
                error_type=error.error_type if error else None,
                error_message=str(error) if error else None,
                fbtrace_id=error.fbtrace_id if error else None,
                request_id=request_id,
            )
        )
