"""Idempotencia de mensajes entrantes — Capa 1 de 3.

Capa 1 (aquí):  SET NX en Redis con TTL 48h. Meta/Twilio reintentan webhooks;
                un message_id ya visto se descarta silenciosamente (200 OK).
Capa 2:         Redis Stream consumer group con MAX_DELIVERIES + dead-letter
                (core/orchestrator/consumer.py).
Capa 3:         UNIQUE en Postgres (messages.idempotency_key + claves de negocio
                que los plugins derivan del ToolContext.idempotency_key).

Fail-closed: si Redis no responde, el error se propaga — preferimos no encolar
a arriesgar una operación de negocio duplicada (mismo criterio que agent-sale).
"""

from __future__ import annotations

from core.memory.working import get_redis

TTL_MS = 48 * 60 * 60 * 1000  # 48 horas: cubre la ventana de reintentos de los proveedores


async def claim_message_once(provider: str, external_message_id: str) -> bool:
    """True si es la primera vez que vemos este mensaje. False = duplicado."""
    r = get_redis()
    result = await r.set(f"inbound:{provider}:{external_message_id}", "1", px=TTL_MS, nx=True)
    return result is True
