"""Salida de Facebook Messenger (Send API de la Página)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from core.config import get_settings
from core.gateway.channels.base import OutboundAdapter
from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.gateway.meta_graph import GRAPH_BASE, appsecret_proof
from core.gateway.meta_messenger import MessengerSendError, plan_de_envio
from core.shared.exceptions import ChannelError


class MessengerMetaOutbound(OutboundAdapter):
    """`credentials` (lo arma `core/gateway/sender.py`): `page_id`, `access_token` (el de la Página),
    `app_secret` (opcional, para `appsecret_proof`), `graph_api_version`, y por mensaje
    `service_window_expires_at` (ISO) y `human` (`"true"` si lo envía una persona)."""

    provider = "meta"
    channel = "messenger"

    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        page_id = credentials.get("page_id", "")
        access_token = credentials.get("access_token", "")
        if not page_id or not access_token:
            raise ChannelError("Conexión de Messenger sin credenciales (page_id/access_token)")

        try:
            plan = plan_de_envio(
                service_window_expires_at=_fecha(credentials.get("service_window_expires_at", "")),
                humano=credentials.get("human") == "true",
            )
        except MessengerSendError as exc:
            # Permanente: reintentar no abre la ventana (mismo criterio que 131047 en WhatsApp).
            raise GraphError(str(exc), category="PERMANENT") from exc

        payload: dict[str, object] = {
            "recipient": {"id": to_external_id.removeprefix("messenger:")},
            "messaging_type": plan.messaging_type,
            "message": {"text": text},
        }
        if plan.tag:
            payload["tag"] = plan.tag
        return await self._post(credentials, page_id, access_token, payload)

    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str:
        # Fuera de alcance de esta entrega: falla explícito y permanente, no en silencio.
        raise GraphError("Envío de imágenes por Messenger no implementado todavía", category="PERMANENT")

    @staticmethod
    async def _post(credentials: dict[str, str], page_id: str, access_token: str, payload: dict[str, object]) -> str:
        version = credentials.get("graph_api_version") or get_settings().graph_api_version
        params: dict[str, str] = {}
        if app_secret := credentials.get("app_secret", ""):
            params["appsecret_proof"] = appsecret_proof(access_token, app_secret)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{GRAPH_BASE}/{version}/{page_id}/messages",
                json=payload,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code >= 400:
            try:
                body = response.json()
            except Exception:
                body = None
            raise classify_and_build_error(
                http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text
            )
        return str((response.json() or {}).get("message_id", ""))


def _fecha(valor: str) -> datetime | None:
    if not valor:
        return None
    try:
        fecha = datetime.fromisoformat(valor)
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=UTC)
