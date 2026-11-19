"""Cliente HTTP de la API de Twilio (ADR-0186).

Auth básica `account_sid:auth_token` y solo contra los hosts fijos de Twilio: una URL de
paginación que apunte a otro host se rechaza, porque llevaría las credenciales afuera. Los SID
que entran a una ruta se validan por formato antes de armar la URL.

Cada error sale como `ProviderError` con su categoría (`errors.py`). Un timeout de lectura en un
POST queda `AMBIGUOUS` (Twilio pudo haber aceptado el envío); en un GET, `TRANSIENT`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from agentcore_channel_twilio.errors import error_from_response
from core.gateway.channels.base import ProviderError
from core.shared.logger import get_logger

log = get_logger(__name__)

API_BASE = "https://api.twilio.com/2010-04-01"
MESSAGING_BASE = "https://messaging.twilio.com/v2"
CONTENT_BASE = "https://content.twilio.com/v1"
ALLOWED_HOSTS = frozenset({"api.twilio.com", "messaging.twilio.com", "content.twilio.com"})

TIMEOUT = httpx.Timeout(10.0, connect=3.0)
# Tope de páginas al listar: una cuenta con más senders o plantillas que esto es un caso a mirar,
# no algo que recorrer sin fin.
MAX_PAGES = 20

_SID = {
    "AC": re.compile(r"^AC[0-9a-f]{32}$"),
    "XE": re.compile(r"^XE[0-9a-f]{32}$"),
    "HX": re.compile(r"^HX[0-9a-f]{32}$"),
    "MG": re.compile(r"^MG[0-9a-f]{32}$"),
    "SM": re.compile(r"^(SM|MM)[0-9a-f]{32}$"),
}

FormData = Mapping[str, str] | Sequence[tuple[str, str]]


def require_sid(value: str, prefix: str) -> str:
    """Devuelve el SID si tiene el formato de Twilio (`AC`, `XE`, `HX`, `MG`, `SM`/`MM` + 32 hex); si no, un
    `ProviderError` CONFIG. Así un valor guardado o recibido nunca altera la ruta de la URL."""
    pattern = _SID[prefix]
    if not isinstance(value, str) or not pattern.match(value):
        raise ProviderError(
            f"SID de Twilio inválido (se esperaba {prefix}…)", provider="twilio", category="CONFIG"
        )
    return value


class TwilioClient:
    """Una cuenta (o subcuenta) de Twilio. Sin estado: se crea por operación."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.account_sid = require_sid(account_sid, "AC")
        if not auth_token:
            raise ProviderError(
                "Falta el Auth Token de Twilio", provider="twilio", category="CONFIG"
            )
        self._auth = httpx.BasicAuth(self.account_sid, auth_token)
        self._transport = transport

    @classmethod
    def from_credentials(
        cls, credentials: Mapping[str, str], *, transport: httpx.AsyncBaseTransport | None = None
    ) -> TwilioClient:
        return cls(
            str(credentials.get("account_sid") or ""),
            str(credentials.get("auth_token") or ""),
            transport=transport,
        )

    @property
    def account_url(self) -> str:
        return f"{API_BASE}/Accounts/{self.account_sid}"

    async def get(
        self, url: str, *, op: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._request("GET", url, op=op, idempotent=True, params=params)

    async def post_form(self, url: str, data: FormData, *, op: str) -> dict[str, Any]:
        # Codificado a mano: `httpx` no acepta una lista de pares en `data=`, y el orden y los
        # campos repetidos (`MediaUrl`) importan.
        pairs = list(data.items()) if isinstance(data, Mapping) else list(data)
        return await self._request(
            "POST",
            url,
            op=op,
            idempotent=False,
            content=urlencode(pairs).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    async def post_json(self, url: str, payload: Mapping[str, Any], *, op: str) -> dict[str, Any]:
        return await self._request("POST", url, op=op, idempotent=False, json=payload)

    async def delete(self, url: str, *, op: str) -> None:
        await self._request("DELETE", url, op=op, idempotent=True)

    async def paginate(
        self, url: str, *, key: str, op: str, params: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Recorre `meta.next_page_url` (formato de las APIs v1/v2) hasta `MAX_PAGES`."""
        items, _completo = await self.paginate_completo(url, key=key, op=op, params=params)
        return items

    async def paginate_completo(
        self, url: str, *, key: str, op: str, params: Mapping[str, Any] | None = None
    ) -> tuple[list[dict[str, Any]], bool]:
        """Como `paginate`, y además si la lista llegó COMPLETA (`False` = se cortó en `MAX_PAGES`). Quien da de baja
        lo que falta (el sync de plantillas) solo debe hacerlo con la lista completa."""
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        page_params = params
        for _ in range(MAX_PAGES):
            if not next_url:
                return items, True
            body = await self.get(next_url, op=op, params=page_params)
            items.extend(body.get(key) or [])
            next_url = (body.get("meta") or {}).get("next_page_url") or None
            page_params = None  # la URL de la página siguiente ya trae sus parámetros
        if not next_url:
            return items, True
        log.warning("twilio_paginate_truncated", op=op, pages=MAX_PAGES)
        return items, False

    async def _request(
        self, method: str, url: str, *, op: str, idempotent: bool, **kwargs: Any
    ) -> dict[str, Any]:
        host = urlsplit(url).hostname
        if urlsplit(url).scheme != "https" or host not in ALLOWED_HOSTS:
            raise ProviderError(
                "URL fuera de la API de Twilio", provider="twilio", category="CONFIG"
            )
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                auth=self._auth, timeout=TIMEOUT, transport=self._transport, follow_redirects=False
            ) as client:
                response = await client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            log.warning("twilio_api_unreachable", op=op, error=type(exc).__name__)
            raise ProviderError(
                "No se pudo conectar con Twilio", provider="twilio", category="TRANSIENT"
            ) from exc
        except httpx.TransportError as exc:
            # Timeout de lectura o corte a mitad de respuesta: en un POST Twilio pudo haberlo hecho.
            category = "TRANSIENT" if idempotent else "AMBIGUOUS"
            log.warning("twilio_api_ambiguous", op=op, error=type(exc).__name__, category=category)
            raise ProviderError(
                "Twilio no respondió a tiempo", provider="twilio", category=category
            ) from exc

        duration_ms = round((time.monotonic() - started) * 1000)
        request_id = response.headers.get("twilio-request-id")
        if response.is_success:
            log.info(
                "twilio_api_call",
                op=op,
                method=method,
                status=response.status_code,
                duration_ms=duration_ms,
                twilio_request_id=request_id,
            )
            if response.status_code == 204 or not response.content:
                return {}
            try:
                body = response.json()
            except ValueError:
                body = None
            if not isinstance(body, dict):
                # 2xx con un cuerpo que no es JSON: en un POST no se sabe si Twilio lo hizo. Clasificado, nunca
                # un `ValueError` suelto que el envío reintentaría como transitorio (y duplicaría).
                category = "TRANSIENT" if idempotent else "AMBIGUOUS"
                log.warning(
                    "twilio_api_cuerpo_invalido",
                    op=op,
                    status=response.status_code,
                    category=category,
                )
                raise ProviderError(
                    "Twilio respondió sin un cuerpo JSON válido",
                    provider="twilio",
                    category=category,
                )
            return body

        error = error_from_response(response)
        log.warning(
            "twilio_api_error",
            op=op,
            method=method,
            status=response.status_code,
            code=error.code,
            category=error.category,
            duration_ms=duration_ms,
            twilio_request_id=request_id,
        )
        raise error
