"""Allowlist de headers logueables, compartida por todo el código que loguea
respuestas HTTP de Meta (`meta_usage.py`, `meta_health.py`, `alerts.py`) --
NUNCA `Authorization`/`Cookie`, por construcción (allowlist, no filtro por
excepción): si Meta agrega un header nuevo mañana, por defecto NO se loguea
hasta que alguien lo agregue acá a propósito.
"""

from __future__ import annotations

import httpx

LOGGABLE_HEADERS = {"x-business-use-case-usage", "x-app-usage", "x-fb-trace-id"}


def safe_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() in LOGGABLE_HEADERS}
