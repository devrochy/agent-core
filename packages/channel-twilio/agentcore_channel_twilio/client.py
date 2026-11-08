"""Cliente HTTP único de las APIs de Twilio (REST 2010-04-01, Senders v2 y Content v1).

Autenticación básica con el Account SID y el Auth Token del negocio (BYOK, ADR-0186). El
token nunca se loguea ni viaja en un mensaje de error. Todo fallo sale como `ProviderError`
ya clasificado (`errors.py`), para que `decide_retry` lo trate igual que a uno de Meta.
"""

from __future__ import annotations

from typing import Any

import httpx

from agentcore_channel_twilio.errors import error_from_response, error_from_transport
from core.gateway.channels.base import ProviderError
from core.shared.exceptions import ChannelError

API_BASE = "https://api.twilio.com/2010-04-01"
MESSAGING_V2_BASE = "https://messaging.twilio.com/v2"
CONTENT_BASE = "https://content.twilio.com/v1"

# Conexión corta (si no conecta, nada salió y se reintenta) y lectura más larga: un timeout de
# lectura deja el envío en duda (AMBIGUOUS), así que conviene que sea raro.
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class TwilioClient:
    """Un cliente por llamada: sin estado entre negocios. `transport` es para los tests."""

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not account_sid or not auth_token:
            raise ChannelError("Conexión Twilio sin credenciales (account_sid/auth_token)")
        self.account_sid = account_sid
        self._auth = httpx.BasicAuth(account_sid, auth_token)
        self._transport = transport

    @classmethod
    def from_credentials(
        cls, credentials: dict[str, str], *, transport: httpx.AsyncBaseTransport | None = None
    ) -> TwilioClient:
        return cls(
            credentials.get("account_sid", ""),
            credentials.get("auth_token", ""),
            transport=transport,
        )

    def api_url(self, path: str) -> str:
        """URL de la API REST de la cuenta: `api_url("Messages.json")`."""
        return f"{API_BASE}/Accounts/{self.account_sid}/{path.lstrip('/')}"

    async def request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Hace la llamada y devuelve el JSON. Un 204 (borrado) devuelve `{}`."""
        try:
            async with httpx.AsyncClient(
                auth=self._auth, timeout=_TIMEOUT, transport=self._transport
            ) as client:
                response = await client.request(method, url, data=data, json=json, params=params)
        except httpx.TransportError as exc:
            raise error_from_transport(exc, method=method) from exc
        if response.status_code >= 400:
            raise error_from_response(response)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            # 2xx sin JSON: el pedido llegó y pudo haberse cumplido.
            raise ProviderError(
                f"Twilio respondió {response.status_code} sin JSON",
                provider="twilio",
                http_status=response.status_code,
                category="TRANSIENT"
                if method.upper() in ("GET", "HEAD", "DELETE")
                else "AMBIGUOUS",
            ) from exc
        return body if isinstance(body, dict) else {"data": body}

    async def fetch_account(self) -> dict[str, Any]:
        """La cuenta (`status`: active, suspended o closed; `type`: Trial o Full). Exige el Auth
        Token o una API key Main: una key Standard no lee `/Accounts`."""
        return await self.request("GET", f"{API_BASE}/Accounts/{self.account_sid}.json")

    async def paginate(
        self, url: str, key: str, *, params: dict[str, str] | None = None, max_pages: int = 20
    ) -> list[dict[str, Any]]:
        """Recorre una lista paginada de las APIs v1/v2 (`meta.next_page_url`) o de la REST
        (`next_page_uri`). `max_pages` corta un bucle si Twilio devolviera siempre la misma página."""
        items: list[dict[str, Any]] = []
        next_url: str | None = url
        first = True
        for _ in range(max_pages):
            if not next_url:
                break
            body = await self.request("GET", next_url, params=params if first else None)
            first = False
            items.extend(x for x in body.get(key) or [] if isinstance(x, dict))
            meta = body.get("meta") or {}
            next_url = meta.get("next_page_url") if isinstance(meta, dict) else None
            if not next_url and body.get("next_page_uri"):
                next_url = f"https://api.twilio.com{body['next_page_uri']}"
        return items
