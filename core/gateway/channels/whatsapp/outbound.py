"""WhatsApp saliente vía Meta Cloud API (Graph API)."""

from __future__ import annotations

from typing import Any

import httpx

from core.gateway.channels.base import OutboundAdapter
from core.gateway.meta_errors import classify_and_build_error
from core.shared.exceptions import ChannelError

GRAPH_API = "https://graph.facebook.com/v21.0"


class WhatsAppMetaOutbound(OutboundAdapter):
    provider = "meta"

    async def send_text(self, credentials: dict[str, str], to_external_id: str, text: str) -> str:
        # external_id canónico: "whatsapp:+573001234567" -> Meta espera dígitos
        to = to_external_id.removeprefix("whatsapp:").lstrip("+")
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        return await self._post(credentials, payload)

    async def send_image(
        self, credentials: dict[str, str], to_external_id: str, image_url: str, caption: str = ""
    ) -> str:
        to = to_external_id.removeprefix("whatsapp:").lstrip("+")
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "image",
            "image": {"link": image_url, "caption": caption},
        }
        return await self._post(credentials, payload)

    async def send_template(
        self,
        credentials: dict[str, str],
        to_external_id: str,
        name: str,
        language: str,
        components: list[dict[str, Any]],
    ) -> str:
        """Plantilla aprobada fuera de la ventana de 24 h (HU-ARCH-13, ADR-0108).
        `components` llega ya resuelto (parámetros del evento o de la BD)."""
        to = to_external_id.removeprefix("whatsapp:").lstrip("+")
        template: dict[str, Any] = {"name": name, "language": {"code": language}}
        if components:
            template["components"] = components
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": template,
        }
        return await self._post(credentials, payload)

    async def send_interactive(
        self, credentials: dict[str, str], to_external_id: str, interactive: dict[str, Any]
    ) -> str:
        """Botones de respuesta o lista dentro de la ventana de 24 h (HU-AOS-09, HU-INMO-03). El id
        de cada botón es el payload que vuelve en `interactive.button_reply.id` (p. ej. el
        `confirmar:` firmado); el de cada fila de lista, en `interactive.list_reply.id`."""
        to = to_external_id.removeprefix("whatsapp:").lstrip("+")
        if interactive.get("type") == "list":
            return await self._post(credentials, self._payload_lista(to, interactive))
        botones = [
            {"type": "reply", "reply": {"id": str(b["id"]), "title": str(b["title"])}}
            for b in interactive.get("buttons") or []
        ]
        if interactive.get("type") != "button" or not botones or not interactive.get("body"):
            raise ChannelError("Mensaje interactivo inválido (se esperan cuerpo y botones)")
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {"type": "button", "body": {"text": str(interactive["body"])}, "action": {"buttons": botones}},
        }
        return await self._post(credentials, payload)

    @staticmethod
    def _payload_lista(to: str, interactive: dict[str, Any]) -> dict[str, Any]:
        """Lista con una sola sección: `action.sections[0].rows[]`. La descripción de fila es
        opcional y se omite si viene vacía (Meta rechaza cadenas vacías)."""
        filas: list[dict[str, str]] = []
        for f in interactive.get("rows") or []:
            fila = {"id": str(f["id"]), "title": str(f["title"])}
            if f.get("description"):
                fila["description"] = str(f["description"])
            filas.append(fila)
        if not filas or not interactive.get("body") or not interactive.get("button"):
            raise ChannelError("Mensaje interactivo inválido (se esperan cuerpo, botón y filas)")
        return {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": str(interactive["body"])},
                "action": {"button": str(interactive["button"]), "sections": [{"rows": filas}]},
            },
        }

    @staticmethod
    async def _post(credentials: dict[str, str], payload: dict[str, object]) -> str:
        phone_number_id = credentials.get("phone_number_id", "")
        access_token = credentials.get("access_token", "")
        if not phone_number_id or not access_token:
            raise ChannelError("Conexión WhatsApp sin credenciales (phone_number_id/access_token)")
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{GRAPH_API}/{phone_number_id}/messages",
                json=payload,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code >= 400:
            # Se re-lanza el GraphError CLASIFICADO (no un ChannelError
            # genérico): la tarea de envío (`core/tasks/outbound_send.py`)
            # necesita el `category`/`code`/`retry_after_ms` reales para
            # decidir si reintenta, cuándo, o si es un fallo permanente
            # (131047, re-engagement) que no vale la pena reintentar.
            try:
                body = response.json()
            except Exception:
                body = None
            raise classify_and_build_error(
                http_status=response.status_code, body=body, headers=response.headers, raw_text=response.text
            )
        data = response.json()
        try:
            message_id = data["messages"][0]["id"]
            return str(message_id)
        except (KeyError, IndexError):
            return ""
