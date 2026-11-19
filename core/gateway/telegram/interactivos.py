"""Botones de Telegram con ids largos (ADR-0199 §7).

`callback_data` admite 1-64 bytes, y los ids de los botones y filas del kernel llegan hasta 256 (`plan:premium`, ids
firmados de las opciones directas). Antes de enviar, un id que no cabe se cambia por `t:<token>` y el original se
guarda en Redis 7 días, con la llave del negocio y del bot. Cuando el cliente toca el botón, el webhook lo resuelve
de vuelta. Un token vencido o desconocido llega sin id: el agente recibe solo el título visible, como un texto.
"""

from __future__ import annotations

import copy
import secrets
import uuid
from typing import Any

from agentcore_channel_telegram.limites import cabe_callback_data

from core.memory.working import get_redis
from core.shared.logger import get_logger

log = get_logger(__name__)

PREFIJO = "t:"
TTL_S = 7 * 24 * 3600


def _clave(tenant_id: uuid.UUID, bot_id: str, token: str) -> str:
    return f"telegram_cb:{tenant_id}:{bot_id}:{token}"


async def preparar(tenant_id: uuid.UUID, bot_id: str, interactive: dict[str, Any]) -> dict[str, Any]:
    """Copia del interactivo con los ids que no caben cambiados por `t:<token>`. Los que caben quedan igual. Un id
    que ya empieza por `t:` también se cambia, para que el webhook no lo confunda con un token."""
    salida = copy.deepcopy(interactive)
    opciones = salida.get("rows") if salida.get("type") == "list" else salida.get("buttons")
    for opcion in opciones or []:
        if not isinstance(opcion, dict) or not isinstance(opcion.get("id"), str):
            continue
        original = opcion["id"]
        if cabe_callback_data(original) and not original.startswith(PREFIJO):
            continue
        token = secrets.token_urlsafe(12)
        await get_redis().set(_clave(tenant_id, bot_id, token), original, ex=TTL_S)
        opcion["id"] = f"{PREFIJO}{token}"
    return salida


async def resolver(tenant_id: uuid.UUID, bot_id: str, data: str) -> str:
    """El id original de un `callback_data`. Si no es un token, el mismo `data`; si el token venció, `""`."""
    if not data.startswith(PREFIJO):
        return data
    original = await get_redis().get(_clave(tenant_id, bot_id, data.removeprefix(PREFIJO)))
    if original is None:
        log.info("telegram.callback.token_vencido", tenant_id=str(tenant_id), bot_id=bot_id)
        return ""
    return original if isinstance(original, str) else original.decode()
