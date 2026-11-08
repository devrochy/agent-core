"""`X-Request-ID` por petición (ADR-0188).

Cada petición HTTP lleva un id: el que manda el cliente si es seguro (letras, dígitos, `._-`, hasta
64), o uno nuevo. Va en la respuesta (`X-Request-ID`) y en todos los logs de la petición
(`request_id`, por los contextvars de structlog), para saltar del error que ve un usuario a sus
líneas en Loki/Kibana y, con el `trace_id`, a la traza en Tempo.

ASGI puro (no `BaseHTTPMiddleware`): no envuelve el cuerpo ni rompe las respuestas en streaming.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import structlog

CABECERA = "x-request-id"
_VALIDO = re.compile(r"[A-Za-z0-9._-]{1,64}")


def request_id_de(valor: str | None) -> str:
    """El id del cliente si es seguro para un log y una cabecera; si no, uno nuevo."""
    if valor and _VALIDO.fullmatch(valor):
        return valor
    return uuid.uuid4().hex


class RequestIdMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        entrante = None
        for nombre, valor in scope.get("headers") or ():
            if nombre == CABECERA.encode():
                entrante = valor.decode("latin-1")
                break
        rid = request_id_de(entrante)

        async def _send(mensaje: dict[str, Any]) -> None:
            if mensaje.get("type") == "http.response.start":
                cabeceras = [(k, v) for k, v in mensaje.get("headers", []) if k.lower() != CABECERA.encode()]
                cabeceras.append((CABECERA.encode(), rid.encode()))
                mensaje = {**mensaje, "headers": cabeceras}
            await send(mensaje)

        with structlog.contextvars.bound_contextvars(request_id=rid):
            await self.app(scope, receive, _send)
