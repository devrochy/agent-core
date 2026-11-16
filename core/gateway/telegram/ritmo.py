"""Ritmo de salida de un bot de Telegram (ADR-0199 §5).

Telegram admite unos 30 mensajes por segundo por bot y pide no pasar de 1 por segundo en un mismo chat. Antes de cada
envío se reserva un lugar en Redis:

- por bot: contador por segundo, tope `RITMO_BOT_POR_SEGUNDO`;
- por chat: un mensaje por segundo;
- pausa del bot: tras un 429, el bot espera los `retry_after` segundos que pidió Telegram.

Si no hay lugar, devuelve los segundos a esperar: el envío sale como `RATE_LIMIT` con ese `retry_after` y
`decide_retry` lo reprograma. Sin Redis no se frena (el envío ya depende de Redis para su candado).
"""

from __future__ import annotations

import time
import uuid

from agentcore_channel_telegram.limites import RITMO_BOT_POR_SEGUNDO

from core.memory.working import get_redis
from core.shared.logger import get_logger

log = get_logger(__name__)


def _pausa(bot_id: str) -> str:
    return f"telegram_ritmo:pausa:{bot_id}"


async def esperar(tenant_id: uuid.UUID, bot_id: str, chat_id: str) -> float:
    """Segundos a esperar antes de enviar (0 = enviar ya). Reserva el lugar si devuelve 0."""
    redis = get_redis()
    try:
        pausa = await redis.ttl(_pausa(bot_id))
        if pausa and pausa > 0:
            return float(pausa)
        if not await redis.set(f"telegram_ritmo:chat:{bot_id}:{chat_id}", "1", nx=True, px=1000):
            return 1.0
        segundo = int(time.time())
        clave = f"telegram_ritmo:bot:{bot_id}:{segundo}"
        usados = await redis.incr(clave)
        if usados == 1:
            await redis.expire(clave, 2)
        if usados > RITMO_BOT_POR_SEGUNDO:
            return 1.0
    except Exception:
        log.warning("telegram.ritmo.sin_redis", tenant_id=str(tenant_id))
    return 0.0


async def pausar(bot_id: str, segundos: float) -> None:
    """Tras un 429: el bot no envía nada durante `retry_after`."""
    try:
        await get_redis().set(_pausa(bot_id), "1", ex=max(1, int(segundos + 0.999)))
    except Exception:
        log.warning("telegram.ritmo.pausa_sin_redis", bot_id=bot_id)
