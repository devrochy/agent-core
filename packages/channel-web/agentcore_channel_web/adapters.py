"""Adaptador saliente del canal `web` (HU-CHAN-02).

A diferencia de WhatsApp/email, aquí NO se llama a ningún proveedor externo: la
respuesta ya está guardada como `Message` y el widget la recoge por polling. El
adaptador solo devuelve un identificador sintético (el `external_message_id`)
para que la tarea de salida marque el mensaje como `sent` sin error.
"""

from __future__ import annotations

from typing import Any

from core.gateway.channels.base import OutboundAdapter


class WebOutbound(OutboundAdapter):
    provider = "web"

    async def send_text(
        self, credentials: dict[str, Any], to_external_id: str, text: str
    ) -> str:
        # El mensaje ya está persistido; el widget lo leerá por polling.
        return f"web:{to_external_id}"

    async def send_image(
        self,
        credentials: dict[str, Any],
        to_external_id: str,
        image_url: str,
        caption: str = "",
    ) -> str:
        # El chat web de Fase 1 es solo texto.
        raise NotImplementedError("El chat web todavía no envía imágenes")
