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
from core.gateway.connections import effective_credentials
from core.gateway.idempotency import claim_message_once
from core.memory.working import get_redis
from core.shared.logger import get_logger
from core.shared.tenant import default_tenant, resolve_tenant_for_phone, tenant_scope

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
    creds = await effective_credentials()
    verify_token = creds.get("verify_token") or settings.whatsapp_verify_token
    if hub_mode == "subscribe" and verify_token and hub_verify_token == verify_token:
        return Response(content=hub_challenge, media_type="text/plain")
    return Response(status_code=403)


@router.post("/whatsapp")
async def receive_whatsapp(request: Request) -> dict[str, Any]:
    settings = get_settings()
    raw_body = await request.body()
    adapter = inbound_adapter_for("meta")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return {"status": "ignored"}

    phone_ids = _payload_phone_number_ids(payload)
    phone_id = phone_ids[0] if phone_ids else ""

    # Tenant del webhook: phone_number_id -> channel_connection -> tenant.
    # SECURITY DEFINER (ADR-0012): se resuelve antes de saber el tenant. Si el
    # número no pertenece a ninguna conexión, cae al primer tenant (dev/bootstrap).
    tenant_id = await resolve_tenant_for_phone(phone_id)
    if tenant_id is None:
        tenant_id = await default_tenant()
    if tenant_id is None:
        log.warning("webhook_without_tenant")
        return {"status": "ignored"}

    async with tenant_scope(tenant_id):
        # La firma se valida con el app_secret de la conexión que matchea el
        # phone_number_id del payload (fallback: primaria activa -> .env).
        creds = await effective_credentials(phone_id)
        app_secret = creds.get("app_secret") or settings.whatsapp_app_secret
        if app_secret:
            headers = {k.lower(): v for k, v in request.headers.items()}
            if not adapter.verify_request(raw_body, headers, app_secret):
                log.warning("webhook_signature_invalid")
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
                    "tenant_id": str(tenant_id),
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


def _payload_phone_number_ids(payload: dict[str, Any]) -> list[str]:
    """Extrae los phone_number_id (clave de ruteo) del payload de Meta."""
    ids: list[str] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            phone_number_id = value.get("metadata", {}).get("phone_number_id", "")
            if phone_number_id and phone_number_id not in ids:
                ids.append(phone_number_id)
    return ids


async def _try_process_media(message: Any) -> Any:
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
