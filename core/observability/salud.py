"""Salud profunda: `GET /healthz/deep` (ADR-0188).

`/healthz` sigue estático: lo usan los health checks de Coolify y de compose, y no debe
tumbar el contenedor de la API porque Redis o el worker fallen. `/healthz/deep` sí mira las
dependencias, para las sondas externas y las alertas:

- `db`: `SELECT 1` con tope de 1 s.
- `redis`: `PING` con tope de 1 s.
- `worker`: edad del latido `worker:heartbeat` que escribe el cron `core.worker_heartbeat`
  cada minuto; viejo (más de `LATIDO_MAX_S`) o ausente = fallo.

Responde 200 si todo está bien y 503 si algo falla, con `{status, componentes}` y solo
`"ok"`/`"fallo"`: nada de DSN, hosts ni mensajes de error, porque la ruta es pública (el
middleware deja pasar todo `/healthz*`). El resultado se guarda 5 s, también el 503, y las
peticiones simultáneas comparten una sola medición: una sonda que insiste no golpea la base.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from core.shared.logger import get_logger

log = get_logger(__name__)

LATIDO_KEY = "worker:heartbeat"
# El mismo umbral que el checklist de producción (`meta_production_readiness`) y que el TTL
# con que el cron escribe el latido.
LATIDO_MAX_S = 180
TOPE_SONDA_S = 1.0
CACHE_S = 5.0

OK = "ok"
FALLO = "fallo"

router = APIRouter()


def _redis() -> Any:
    from core.memory.working import get_redis

    return get_redis()


async def sonda_db() -> None:
    from sqlalchemy import text

    from core.db.session import get_engine

    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))


async def sonda_redis() -> None:
    await _redis().ping()


async def edad_del_latido(redis: Any = None) -> float | None:
    """Segundos desde el último latido del worker; `None` si no hay latido legible."""
    crudo = await (redis or _redis()).get(LATIDO_KEY)
    if not crudo:
        return None
    try:
        momento = datetime.fromisoformat(crudo if isinstance(crudo, str) else crudo.decode())
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - momento).total_seconds())


async def sonda_worker() -> None:
    edad = await edad_del_latido()
    if edad is None or edad > LATIDO_MAX_S:
        raise RuntimeError("latido ausente o viejo")


SONDAS: dict[str, Callable[[], Awaitable[None]]] = {
    "db": sonda_db,
    "redis": sonda_redis,
    "worker": sonda_worker,
}


async def _medir(nombre: str, sonda: Callable[[], Awaitable[None]]) -> str:
    try:
        await asyncio.wait_for(sonda(), timeout=TOPE_SONDA_S)
    except Exception as exc:  # cualquier fallo es "fallo"; el detalle no sale de aquí
        log.warning("salud_componente_fallo", componente=nombre, error_type=type(exc).__name__)
        return FALLO
    return OK


async def medir_componentes() -> dict[str, str]:
    nombres = list(SONDAS)
    estados = await asyncio.gather(*(_medir(n, SONDAS[n]) for n in nombres))
    return dict(zip(nombres, estados, strict=True))


_cache: tuple[float, dict[str, str]] | None = None
_candado = asyncio.Lock()


async def componentes_cacheados() -> dict[str, str]:
    global _cache
    if _cache is not None and time.monotonic() - _cache[0] < CACHE_S:
        return _cache[1]
    async with _candado:
        if _cache is not None and time.monotonic() - _cache[0] < CACHE_S:
            return _cache[1]  # otra petición midió mientras esta esperaba
        componentes = await medir_componentes()
        _cache = (time.monotonic(), componentes)
        return componentes


@router.get("/healthz/deep", include_in_schema=False)
async def healthz_deep() -> JSONResponse:
    componentes = await componentes_cacheados()
    sano = all(estado == OK for estado in componentes.values())
    return JSONResponse(
        {"status": OK if sano else FALLO, "componentes": componentes},
        status_code=200 if sano else 503,
        headers={"Cache-Control": "no-store"},
    )
