"""Endpoints de webhooks de canales.

Flujo (patrón gateway -> queue -> consumer de agent-sale):
1. Verificar firma (HMAC-SHA256 con App Secret).
2. Parsear payload -> InboundMessage[].
3. Deduplicar por message_id (Redis SET NX, 48h).
4. Procesar medios si mod-media-processor está activo (in-process).
5. Encolar en Redis Stream y responder 200 INMEDIATAMENTE.

El LLM nunca corre dentro del request del webhook.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Query, Request, Response

from core.config import get_settings
from core.gateway.channels.registry import inbound_adapter_for
from core.gateway.idempotency import claim_message_once
from core.memory.working import get_redis
from core.shared.logger import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

INBOUND_STREAM = "agent_core:inbound"


@router.get("/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
) -> Response:
    """Handshake de verificación de Meta."""
    settings = get_settings()
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


@router.post("/whatsapp")
async def receive_whatsapp(request: Request) -> dict[str, Any]:
    settings = get_settings()
    raw_body = await request.body()
    adapter = inbound_adapter_for("meta")

    if settings.whatsapp_app_secret:
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not adapter.verify_request(raw_body, headers, settings.whatsapp_app_secret):
            log.warning("webhook_signature_invalid")
            return {"status": "ignored"}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return {"status": "ignored"}

    messages = adapter.parse_inbound(payload)
    enqueued = 0
    duplicates = 0

    redis = get_redis()
    for message in messages:
        if not await claim_message_once(message.provider, message.external_message_id):
            duplicates += 1
            continue

        # Procesamiento de medios in-process (si el plugin está activo)
        if message.media:
            message = await _try_process_media(message)

        await redis.xadd(
            INBOUND_STREAM,
            {
                "provider": message.provider,
                "channel": message.channel,
                "external_message_id": message.external_message_id,
                "customer_external_id": message.customer_external_id,
                "customer_name": message.customer_name or "",
                "content": message.content,
                "connection_external_id": message.connection_external_id,
                "media": json.dumps(message.media),
                "media_context": message.media_context,
            },
        )
        enqueued += 1

    return {"status": "ok", "enqueued": enqueued, "duplicates": duplicates}


async def _try_process_media(message):  # type: ignore[no-untyped-def]
    """Si mod-media-processor está activo, transforma audio/imagen a texto ANTES de encolar.

    Llamada de función Python en el mismo proceso — cero red (docs/MONOLITH-MODEL.md).
    """
    from core.plugin_manager import plugin_manager

    module = plugin_manager.get("mod_media_processor")
    if module is None or not plugin_manager.is_active("mod_media_processor"):
        if not message.content:
            message.content = "[El cliente envió un archivo multimedia que no puedo procesar]"
        return message
    try:
        processed = await module.process_media(message)  # type: ignore[attr-defined]
        return processed
    except Exception as exc:
        log.exception("media_processing_failed")
        await plugin_manager.auto_disable("mod_media_processor", str(exc))
        return message
