"""Adaptadores del canal `waweb` (ADR-0074).

`InboundAdapter` es un no-op: el canal no tiene webhook HTTP (el socket del
`connector` construye `InboundMessage` y lo publica al stream del core). Existe
para cumplir el contrato del registry (`agent_core.channels`).

`OutboundAdapter` delega el envío al core (`core.waweb.handoff`), que hace el
request/reply por Redis contra el proceso `connector`.
"""

from __future__ import annotations

from typing import Any

from core.gateway.channels.base import InboundAdapter, InboundMessage, OutboundAdapter
from core.shared.tenant import get_current_tenant_id
from core.waweb.handoff import WaWebError, request_send


class WaWebInbound(InboundAdapter):
    provider = "waweb"
    channel = "whatsapp"

    def verify_request(self, raw_body: bytes, headers: dict[str, str], secret: str) -> bool:
        # El canal no recibe webhooks: no hay firma que verificar. El inbound lo
        # inyecta el proceso `connector` directamente al stream del core.
        return True

    def parse_inbound(self, payload: dict[str, Any]) -> list[InboundMessage]:
        return []


class WaWebOutbound(OutboundAdapter):
    provider = "waweb"

    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        return await self._send(credentials, to_external_id, text, image_url=None)

    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str:
        return await self._send(credentials, to_external_id, caption, image_url=image_url)

    @staticmethod
    async def _send(
        credentials: dict[str, str], to_external_id: str, text: str, image_url: str | None
    ) -> str:
        tenant_id = get_current_tenant_id()
        if tenant_id is None:
            raise WaWebError("Sin tenant en contexto para enviar por waweb")
        to = to_external_id.removeprefix("whatsapp:").lstrip("+")
        return await request_send(
            tenant_id=str(tenant_id),
            connection_id=credentials.get("connection_id") or None,
            to=to,
            text=text,
            image_url=image_url,
        )
