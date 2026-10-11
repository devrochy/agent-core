"""Capa 1 — Working Memory (Redis).

Contexto inmediato de la conversación: últimos N turnos, para armar el
historial del LLM sin golpear Postgres en cada turno. Degrada con gracia:
si Redis falla, el orchestrator reconstruye desde Postgres (Capa 2 de messages).

Clave: wm:{tenant_id}:{conversation_id} (wm:{conversation_id} sin tenant en contexto)
-> RPUSH de JSON {role, content, tool_calls}
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


async def clear_many(conversation_ids: list[uuid.UUID]) -> int:
    """Borra la memoria de trabajo de varias conversaciones del negocio en contexto.

    La usa la purga de un cliente (todas sus conversaciones). Borra la clave con el negocio
    (`wm:{tenant_id}:{conv}`) y la legada (`wm:{conv}`, solo id de conversación: no es de otro
    negocio). Devuelve cuántas claves borró; si Redis falla lo registra y devuelve 0 (la
    memoria de trabajo vence sola en 24 h).
    """
    from core.shared.tenant import get_current_tenant_id

    tid = get_current_tenant_id()
    claves = [f"wm:{c}" for c in conversation_ids]
    if tid is not None:
        claves += [f"wm:{tid}:{c}" for c in conversation_ids]
    borradas = 0
    try:
        r = get_redis()
        for i in range(0, len(claves), 500):
            borradas += int(await r.unlink(*claves[i : i + 500]))
    except Exception:
        log.error("working_memory_clear_many_failed", conversations=len(conversation_ids), exc_info=True)
    return borradas
