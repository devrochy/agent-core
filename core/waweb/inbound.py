"""Publicación del inbound de `waweb` al stream del core (ADR-0074).

El socket vive en el proceso `connector`; el core consume del MISMO stream que
usa el webhook de Meta (`agent_core:inbound`), en el mismo formato. Así el
mensaje entra al pipeline normal (consumer → debounce → loop → sender) sin tocar
el core.

Idempotencia Capa 1: se reclama por `(tenant, provider="waweb", wamid)` antes de
encolar, igual que el webhook de Meta.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from core.gateway.idempotency import claim_message_once
from core.memory.working import get_redis


def _inbound_stream() -> str:
    from core.gateway.webhooks import INBOUND_STREAM

    return INBOUND_STREAM


async def publish_inbound(
    *,
    tenant_id: uuid.UUID,
    connection_external_id: str,
    sender: str,
    text: str,
    wamid: str,
    customer_name: str | None = None,
    media: list[dict[str, str]] | None = None,
    media_context: str = "",
) -> bool:
    """Encola un mensaje entrante. `False` si era duplicado o no había contenido."""
    if not wamid or not (text or media):
        return False
    if not await claim_message_once(tenant_id, "waweb", wamid):
        return False

    fields: dict[str, Any] = {
        "tenant_id": str(tenant_id),
        "provider": "waweb",
        "channel": "whatsapp",
        "external_message_id": wamid,
        "customer_external_id": f"whatsapp:+{sender.lstrip('+')}",
        "customer_name": customer_name or "",
        "content": text,
        "connection_external_id": connection_external_id,
        "media": json.dumps(media or []),
        "media_context": media_context,
    }
    await get_redis().xadd(_inbound_stream(), fields)
    return True
