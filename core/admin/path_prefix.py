"""Soporte de despliegue bajo prefijo de ruta (ADR-0073).

Cuando agent-core se sirve detrás de un proxy inverso que lo monta bajo un
prefijo (p. ej. `/agent-core`), el proxy **quita** el prefijo antes de reenviar,
así que la app recibe rutas limpias (`/admin/...`). Pero las URLs que la app
**genera** (redirects con `Location`, links `href`/`src`/`action` y algunos
`fetch`) son absolutas de raíz y el navegador las resolvería contra la raíz del
dominio (otro servicio).

`PathPrefixMiddleware` reescribe esas URLs salientes agregando el prefijo. Es la
contraparte de entrada del proxy: no toca el routing (el proxy ya quitó el
prefijo), solo la salida.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# Una sola pasada para no prefijar dos veces (p. ej. `location.href` contiene
# `href`): alternancia con el token más específico. El lookahead `(?!/)` respeta
# las URLs protocolo-relativas (`//cdn`).
_URL_RE = re.compile(
    r"(?P<head>"
    r"\b(?:href|src|action)=[\"']"          # atributos HTML
    r"|\.action\s*=\s*[\"']"                 # form.action = "..."
    r"|location\.(?:href\s*=\s*|assign\(\s*|replace\(\s*)[\"']"  # window.location...
    r"|\bfetch\(\s*[\"']"                    # fetch("/...")
    r")/(?P<path>(?!/)[^\"']*)"
)


def rewrite_html(text: str, prefix: str) -> str:
    """Prefija las URLs absolutas de raíz de un documento HTML."""
    if not prefix:
        return text
    bare = prefix.strip("/")

    def _sub(match: re.Match[str]) -> str:
        path = match.group("path")
        if path == bare or path.startswith(bare + "/"):
            return match.group(0)  # ya prefijada: no duplicar
        return f'{match.group("head")}{prefix}/{path}'

    return _URL_RE.sub(_sub, text)


class PathPrefixMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, prefix: str) -> None:
        super().__init__(app)
        stripped = (prefix or "").strip().strip("/")
        self.prefix = f"/{stripped}" if stripped else ""

    async def dispatch(self, request: Any, call_next: Callable[..., Any]) -> Response:
        response = await call_next(request)
        if not self.prefix:
            return response

        loc = response.headers.get("location")
        if loc and loc.startswith("/") and not loc.startswith("//"):
            response.headers["location"] = self.prefix + loc

        if response.headers.get("content-type", "").startswith("text/html"):
            body = b"".join([chunk async for chunk in response.body_iterator])
            new = Response(
                content=rewrite_html(body.decode("utf-8", "replace"), self.prefix),
                status_code=response.status_code,
                media_type=response.media_type or "text/html",
            )
            for key, value in response.headers.items():
                if key.lower() in ("content-length", "content-type"):
                    continue
                new.headers[key] = value
            return new
        return response
