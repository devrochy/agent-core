"""Publicación del inbound de `waweb` al stream del core (ADR-0074).

El socket vive en el proceso `connector`; el core consume del MISMO stream que
usa el webhook de Meta (`agent_core:inbound`), en el mismo formato. Así el
mensaje entra al pipeline normal (consumer → debounce → loop → sender) sin tocar
el core.

Idempotencia Capa 1: se reclama por `(tenant, provider="waweb", wamid)` antes de
encolar, igual que el webhook de Meta.
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from core.gateway.idempotency import claim_message_once
from core.memory.working import get_redis
from core.shared.logger import get_logger

log = get_logger(__name__)

# Los bytes del media viajan por Redis, no por el stream: el socket ya los
# descargó y el consumer los lee al procesar el turno. 6 h cubre reintentos
# normales del consumer sin dejar binarios colgados para siempre.
_MEDIA_PREFIX = "waweb:media:"
_MEDIA_TTL_S = 6 * 3600


def _inbound_stream() -> str:
    from core.gateway.webhooks import INBOUND_STREAM

    return INBOUND_STREAM


def _descriptor(item: dict[str, Any]) -> dict[str, Any]:
    """Descriptor serializable del media (sin los bytes)."""
    entrada: dict[str, Any] = {
        "type": str(item.get("type") or "").split(";", 1)[0].strip().lower(),
    }
    for clave in ("seconds", "ptt", "caption"):
        if item.get(clave):
            entrada[clave] = item[clave]
    if item.get("error"):
        entrada["error"] = str(item["error"])
    return entrada


async def _materializar_media(
    tenant_id: uuid.UUID, wamid: str, items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Guarda los bytes en Redis y devuelve los descriptores para el stream."""
    descriptores: list[dict[str, Any]] = []
    for indice, item in enumerate(items):
        entrada = _descriptor(item)
        datos = item.get("data")
        if isinstance(datos, (bytes, bytearray)) and datos:
            clave = f"{_MEDIA_PREFIX}{tenant_id}:{wamid}:{indice}"
            try:
                await get_redis().set(
                    clave,
                    base64.b64encode(bytes(datos)).decode("ascii"),
                    ex=_MEDIA_TTL_S,
                )
            except Exception:
                log.warning("waweb_media_store_failed", wamid=wamid, exc_info=True)
                entrada["error"] = "store_failed"
            else:
                entrada["redis_key"] = clave
                entrada["bytes"] = len(datos)
        descriptores.append(entrada)
    return descriptores


async def publish_inbound(
    *,
    tenant_id: uuid.UUID,
    connection_external_id: str,
    sender: str,
    text: str,
    wamid: str,
    customer_name: str | None = None,
    media: list[dict[str, Any]] | None = None,
    media_context: str = "",
) -> bool:
    """Encola un mensaje entrante. `False` si era duplicado o no había contenido. La media
    entra siempre: el crédito del mes la cobra (ADR-0208 §1, sin tope de audios ni imágenes).

    Los bytes de `media` se guardan en Redis y en el stream viaja solo el
    descriptor (`{type, redis_key, bytes}`); así una nota de voz o una foto no
    infla el stream ni obliga a compartir el volumen de storage con el connector.
    """
    if not wamid or not (text or media):
        return False
    if not await claim_message_once(tenant_id, "waweb", wamid):
        return False

    items = media or []
    descriptores = await _materializar_media(tenant_id, wamid, items)

    fields: dict[str, Any] = {
        "tenant_id": str(tenant_id),
        "provider": "waweb",
        "channel": "whatsapp",
        "external_message_id": wamid,
        "customer_external_id": f"whatsapp:+{sender.lstrip('+')}",
        "customer_name": customer_name or "",
        "content": text,
        "connection_external_id": connection_external_id,
        "media": json.dumps(descriptores),
        "media_context": media_context,
    }
    await get_redis().xadd(_inbound_stream(), fields)
    return True
