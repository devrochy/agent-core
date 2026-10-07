"""Handoff de salida hacia el proceso `connector` por Redis (ADR-0074).

El adaptador corre en el worker Celery; el socket de Neonize vive en el proceso
`connector`. El envío cruza como request/reply:

  worker  --RPUSH waweb:outbox:<tenant>-->  connector (drena con delay anti-baneo)
  worker  <--RPUSH waweb:reply:<rid>------  connector (wamid o error)

Así se preservan el reintento/DLQ y el estado de `Message` de
`core/tasks/outbound_send.py` sin tocar esa tarea.

El protocolo vive en el core (dueño del connector); el paquete de canal
(`agentcore-channel-waweb`) solo mapea `OutboundAdapter.send_text` a
`request_send`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from core.shared.logger import get_logger

log = get_logger(__name__)

REPLY_TIMEOUT_S = 45
_OUTBOX_PREFIX = "waweb:outbox:"
_REPLY_PREFIX = "waweb:reply:"
_QR_PREFIX = "waweb:qr:"
_READY_PREFIX = "waweb:ready:"
_RESET_PREFIX = "waweb:reset:"
READY_TTL_S = 120


class WaWebError(Exception):
    """Falla de envío por el canal waweb (se reintenta como transitoria)."""


class WaWebPolicyBlocked(WaWebError):
    """Rechazo recuperable por la política anti-baneo (pausa o tope diario).

    No es un fallo de envío: el mensaje sigue válido y el worker lo reprograma
    para cuando el envío vuelva a estar permitido (ADR-0103)."""


def outbox_key(tenant_id: str) -> str:
    return f"{_OUTBOX_PREFIX}{tenant_id}"


def reply_key(request_id: str) -> str:
    return f"{_REPLY_PREFIX}{request_id}"


def qr_key(tenant_id: str) -> str:
    return f"{_QR_PREFIX}{tenant_id}"


def ready_key(tenant_id: str) -> str:
    return f"{_READY_PREFIX}{tenant_id}"


def reset_key(tenant_id: str) -> str:
    return f"{_RESET_PREFIX}{tenant_id}"


def _redis():  # type: ignore[no-untyped-def]
    from core.memory.working import get_redis

    return get_redis()


def get_redis():  # type: ignore[no-untyped-def]
    """Acceso público al cliente Redis (para el drenaje del connector)."""
    return _redis()


async def request_send(
    *,
    tenant_id: str,
    connection_id: str | None,
    to: str,
    text: str,
    image_url: str | None = None,
) -> str:
    """Encola un envío y espera el wamid del connector.

    Devuelve el `wamid` (id de mensaje del proveedor). Levanta `WaWebError` si el
    connector rechaza o no responde a tiempo (el caller lo trata como transitorio
    y reintenta con backoff).
    """
    request_id = uuid.uuid4().hex
    job = {
        "request_id": request_id,
        "connection_id": connection_id or "",
        "to": to,
        "text": text,
        "image_url": image_url or "",
        "kind": "image" if image_url else "text",
    }
    if not await is_ready(tenant_id):
        # Fail-fast: sin sesión conectada, mejor reintentar (con backoff) que
        # encolar un envío que se va a quedar colgado 45s.
        raise WaWebError("WhatsApp (waweb) no está conectado para este negocio")
    r = _redis()
    await r.rpush(outbox_key(tenant_id), json.dumps(job))
    popped = await r.blpop(reply_key(request_id), timeout=REPLY_TIMEOUT_S)
    if popped is None:
        log.warning("waweb_send_timeout", tenant_id=tenant_id, connection_id=connection_id)
        raise WaWebError("El connector de WhatsApp no respondió a tiempo")
    _key, raw = popped
    try:
        reply: dict[str, Any] = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise WaWebError("Respuesta inválida del connector") from exc
    if not reply.get("ok"):
        mensaje = str(reply.get("error") or "el connector no pudo enviar")
        if reply.get("reason") == "policy":
            raise WaWebPolicyBlocked(mensaje)
        raise WaWebError(mensaje)
    return str(reply.get("wamid") or "")


async def publish_qr(tenant_id: str, qr: str, ttl_s: int = 180) -> None:
    """Publica el QR vigente para que el panel lo muestre (lo usa el connector)."""
    await _redis().set(qr_key(tenant_id), qr, ex=ttl_s)


async def read_qr(tenant_id: str) -> str | None:
    value = await _redis().get(qr_key(tenant_id))
    return value if isinstance(value, str) and value else None


async def clear_qr(tenant_id: str) -> None:
    await _redis().delete(qr_key(tenant_id))


async def set_ready(tenant_id: str, ttl_s: int = READY_TTL_S) -> None:
    """Marca la sesión como conectada (el manager la refresca mientras vive)."""
    await _redis().set(ready_key(tenant_id), "1", ex=ttl_s)


async def clear_ready(tenant_id: str) -> None:
    await _redis().delete(ready_key(tenant_id))


async def is_ready(tenant_id: str) -> bool:
    return bool(await _redis().get(ready_key(tenant_id)))


async def request_reset(tenant_id: str, ttl_s: int = 300) -> None:
    """Pide al connector que recicle la sesión (para re-emitir QR).

    La escribe el panel (`reset_session`); el manager la consume y cierra el
    socket, de modo que `run_session` vuelve a arrancar en `pending_qr`, limpia
    credenciales y publica un QR nuevo. Sin esta señal, un reset con el socket
    vivo no surte efecto (incidente 2026-10-04: el panel no mostraba QR).
    """
    await _redis().set(reset_key(tenant_id), "1", ex=ttl_s)


async def consume_reset(tenant_id: str) -> bool:
    """True (y borra la señal) si el panel pidió reciclar esta sesión."""
    return bool(await _redis().delete(reset_key(tenant_id)))
