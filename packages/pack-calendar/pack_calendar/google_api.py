"""Llamadas HTTP a Google (Calendar API y OAuth2) por el cliente acotado del core.

Invariante de red (ADR-0052): el pack no usa `httpx` directo. Las tools reciben
`context.http` (el core lo arma con `plugin_manager.scoped_http`); el panel, los
jobs y el OAuth, que no tienen `ToolContext`, usan `plugin_http(NETWORK)`: el mismo
constructor y la misma allowlist (`NETWORK` es la `network_allowlist` del módulo).
Un error de red, un dominio fuera de la allowlist o un 4xx/5xx de Google se
convierten en `PluginError`, como antes con `httpx`.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from core.mcp.types import ScopedHttp, plugin_http
from core.shared.exceptions import PluginError

PLUGIN_ID = "pack_calendar"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
# Hosts a los que el servidor sale (Calendar API + userinfo, y el token endpoint).
# `accounts.google.com` solo lo abre el navegador del usuario (pantalla de consentimiento).
NETWORK = ("www.googleapis.com", "oauth2.googleapis.com")
TIMEOUT = 30.0


def default_http() -> ScopedHttp:
    """Cliente acotado del pack para rutas sin `ToolContext` (panel, jobs, OAuth)."""
    return plugin_http(NETWORK)


def tool_http(context: Any) -> ScopedHttp:
    """`context.http` de la tool; si el llamador no lo trae, el cliente del pack."""
    return getattr(context, "http", None) or default_http()


def snippet(data: Any, limit: int = 200) -> str:
    """Primeros caracteres del cuerpo de la respuesta (para mensajes de error)."""
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    return text[:limit]


async def request(
    http: ScopedHttp,
    method: str,
    url: str,
    *,
    token: str | None = None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    timeout: float = TIMEOUT,
) -> dict[str, Any]:
    """`{"status", "data"}` de Google. Solo lanza si la llamada no llegó a Google."""
    result = await http.request(
        method,
        url,
        params=params,
        json_body=json_body,
        data=data,
        headers={"Authorization": f"Bearer {token}"} if token else None,
        timeout=timeout,
    )
    if "error" in result:
        raise PluginError(PLUGIN_ID, f"Google: {result['error']}")
    return result


async def calendar_call(
    http: ScopedHttp,
    method: str,
    path: str,
    token: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    detail: bool = False,
) -> Any:
    """Llamada a la Calendar API; un 4xx/5xx es `PluginError("Calendar API <status>")`.

    `detail` agrega el comienzo del cuerpo al mensaje (lo hacía la consulta de
    disponibilidad).
    """
    result = await request(
        http, method, f"{CALENDAR_API}{path}", token=token, params=params, json_body=json_body
    )
    status = int(result["status"])
    if status >= 400:
        suffix = f": {snippet(result['data'])}" if detail else ""
        raise PluginError(PLUGIN_ID, f"Calendar API {status}{suffix}")
    return result["data"]


def event_path(calendar_id: str, google_event_id: str | None = None) -> str:
    """Ruta del evento con los ids escapados: un `/`, `?` o `..` no cambia la ruta."""
    path = f"/calendars/{quote(calendar_id, safe='@')}/events"
    return f"{path}/{quote(google_event_id, safe='')}" if google_event_id else path


async def create_event(http: ScopedHttp, calendar_id: str, token: str, body: dict[str, Any]) -> dict[str, Any]:
    event: dict[str, Any] = await calendar_call(http, "POST", event_path(calendar_id), token, json_body=body)
    return event


async def patch_event(
    http: ScopedHttp, calendar_id: str, token: str, google_event_id: str, body: dict[str, Any]
) -> dict[str, Any]:
    event: dict[str, Any] = await calendar_call(
        http, "PATCH", event_path(calendar_id, google_event_id), token, json_body=body
    )
    return event


async def delete_event(http: ScopedHttp, calendar_id: str, token: str, google_event_id: str) -> None:
    await calendar_call(http, "DELETE", event_path(calendar_id, google_event_id), token)


async def list_events(
    http: ScopedHttp, calendar_id: str, token: str, params: dict[str, Any], *, detail: bool = False
) -> list[dict[str, Any]]:
    data = await calendar_call(http, "GET", event_path(calendar_id), token, params=params, detail=detail)
    items: list[dict[str, Any]] = (data or {}).get("items", []) if isinstance(data, dict) else []
    return items
