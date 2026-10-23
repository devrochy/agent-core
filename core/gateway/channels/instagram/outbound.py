"""Salida de Instagram: por la Página vinculada (Facebook Login) o por la propia cuenta (Instagram Login)."""

from __future__ import annotations

from core.config import get_settings
from core.gateway.channels.messenger.outbound import MessengerMetaOutbound, _fecha
from core.gateway.meta_errors import GraphError
from core.gateway.meta_instagram import (
    LOGIN_ROUTE_INSTAGRAM,
    InstagramSendError,
    igsid_de,
    validar_texto,
)
from core.gateway.meta_instagram_login import enviar_mensaje
from core.gateway.meta_messenger import MessengerSendError, plan_de_envio
from core.shared.exceptions import ChannelError


class InstagramMetaOutbound(MessengerMetaOutbound):
    """Mismo `POST /{page_id}/messages` con el token de la Página vinculada; cambia el destinatario (IGSID),
    el límite de texto y el nombre del canal en los errores. `credentials` lo arma `core/gateway/sender.py`:
    `page_id` (la Página vinculada a la cuenta), `access_token` (el de esa Página), `app_secret` opcional,
    `graph_api_version` y por mensaje `service_window_expires_at` y `human`."""

    provider = "meta"
    channel = "instagram"

    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        por_instagram_login = credentials.get("login_route") == LOGIN_ROUTE_INSTAGRAM
        page_id = credentials.get("ig_user_id", "") if por_instagram_login else credentials.get("page_id", "")
        access_token = credentials.get("access_token", "")
        if not page_id or not access_token:
            raise ChannelError("Conexión de Instagram sin credenciales (cuenta o token)")

        try:
            validar_texto(text)
            plan = plan_de_envio(
                service_window_expires_at=_fecha(credentials.get("service_window_expires_at", "")),
                humano=credentials.get("human") == "true",
                canal="instagram",
            )
        except (InstagramSendError, MessengerSendError) as exc:
            # Permanente: reintentar no abre la ventana ni acorta el texto.
            raise GraphError(str(exc), category="PERMANENT") from exc

        payload: dict[str, object] = {
            "recipient": {"id": igsid_de(to_external_id)},
            "messaging_type": plan.messaging_type,
            "message": {"text": text},
        }
        if plan.tag:
            payload["tag"] = plan.tag
        if por_instagram_login:
            # Instagram Login: `graph.instagram.com/{ig_user_id}/messages` con el token de la cuenta.
            version = credentials.get("graph_api_version") or get_settings().graph_api_version
            return await enviar_mensaje(page_id, access_token, version, payload)
        return await self._post(credentials, page_id, access_token, payload)

    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str:
        raise GraphError("Envío de imágenes por Instagram no implementado todavía", category="PERMANENT")
