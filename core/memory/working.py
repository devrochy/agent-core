"""Capa 1 — Working Memory (Redis).

Contexto inmediato de la conversación: últimos N turnos, para armar el
historial del LLM sin golpear Postgres en cada turno. Degrada con gracia:
si Redis falla, el orchestrator reconstruye desde Postgres (Capa 2 de messages).

Clave: wm:{conversation_id} -> LPUSH de JSON {role, content, tool_calls}
TTL: 24h (una conversación inactiva un día completo rearma desde Postgres).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import redis.asyncio as aioredis

from core.config import get_settings
from core.shared.logger import get_logger

log = get_logger(__name__)

WORKING_MEMORY_TTL_S = 86_400
MAX_TURNS = 30

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


def _key(conversation_id: uuid.UUID) -> str:
    """Clave Redis con namespace de tenant (defensa en profundidad, ADR-0012).

    Sin el tenant, un `conversation_id` filtrado permitiría leer/escribir memoria
    de otro tenant. El UUID ya lo hace improbable, pero el aislamiento no debe
    depender solo de eso.
    """
    from core.shared.tenant import get_current_tenant_id

    tid = get_current_tenant_id()
    return f"wm:{tid}:{conversation_id}" if tid else f"wm:{conversation_id}"


async def append_turn(
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    max_turns: int | None = None,
) -> None:
    try:
        r = get_redis()
        entry = json.dumps({"role": role, "content": content, "tool_calls": tool_calls or []})
        limit = max_turns if max_turns and max_turns > 0 else MAX_TURNS
        pipe = r.pipeline()
        pipe.rpush(_key(conversation_id), entry)
        pipe.ltrim(_key(conversation_id), -limit, -1)
        pipe.expire(_key(conversation_id), WORKING_MEMORY_TTL_S)
        await pipe.execute()
    except Exception:
        log.warning("working_memory_append_failed", conversation_id=str(conversation_id))


async def get_recent_turns(conversation_id: uuid.UUID) -> list[dict[str, Any]]:
    try:
        r = get_redis()
        raw = await r.lrange(_key(conversation_id), 0, -1)
        return [json.loads(item) for item in raw]
    except Exception:
        log.warning("working_memory_read_failed", conversation_id=str(conversation_id))
        return []


async def clear(conversation_id: uuid.UUID) -> None:
    try:
        await get_redis().delete(_key(conversation_id))
    except Exception:
        log.warning("working_memory_clear_failed", conversation_id=str(conversation_id))
