"""Adaptador saliente del canal `web` (HU-CHAN-02).

A diferencia de WhatsApp/email, aquí NO se llama a ningún proveedor externo: la
respuesta ya está guardada como `Message` y el widget la recoge por polling. El
adaptador solo devuelve un identificador sintético (el `external_message_id`)
para que la tarea de salida marque el mensaje como `sent` sin error.

El identificador es ÚNICO por mensaje: `messages` tiene un índice único por
(tenant, `external_message_id`). Antes era `web:<visitante>`, el mismo para todos
los mensajes de una conversación: el segundo envío chocaba con el índice, la tarea
fallaba al marcarlo `sent` y ese mensaje y todos los siguientes quedaban `queued`
(smoke de test 2026-10-20: en cada conversación del chat web solo el primer
saliente llegaba a `sent`).
"""

from __future__ import annotations

import uuid
from typing import Any

from core.gateway.channels.base import OutboundAdapter


class WebOutbound(OutboundAdapter):
    provider = "web"

    async def send_text(
        self, credentials: dict[str, Any], to_external_id: str, text: str
    ) -> str:
        # El mensaje ya está persistido; el widget lo leerá por polling.
        return f"web:{uuid.uuid4().hex}"

    async def send_image(
        self,
        credentials: dict[str, Any],
        to_external_id: str,
        image_url: str,
        caption: str = "",
    ) -> str:
        # El chat web de Fase 1 es solo texto.
        raise NotImplementedError("El chat web todavía no envía imágenes")
