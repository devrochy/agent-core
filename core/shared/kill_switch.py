"""Lector del interruptor global de la plataforma (HU-ARCH-38, ADR-0200).

Lo lee el camino caliente (entrada del turno, guardián de envío, crons y recuperación de pendientes) con una
caché por proceso de `TTL_S` segundos: activarlo detiene todo en ≤ `TTL_S` sin desplegar. **Falla cerrado**:
si no se puede leer la fila, se responde «activo», queda el evento de error y una alerta. El fallo se guarda
en caché poco tiempo para no martillar una base caída.

Sin `asyncio.Lock`: Celery corre en el loop persistente de `async_bridge` y un lock atado a un loop se
rompería; una consulta cada pocos segundos por proceso es despreciable.
"""

from __future__ import annotations

import time

from sqlalchemy import select

from core.shared.logger import get_logger

log = get_logger(__name__)

MOTIVO_KILL_SWITCH = "kill_switch"
TTL_S = 5.0
TTL_FALLO_S = 1.0

_cache: tuple[bool, float] | None = None  # (activo, expira según time.monotonic)


async def global_activo() -> bool:
    """True si el interruptor global está activo (o si no se pudo leer: falla cerrado)."""
    global _cache
    ahora = time.monotonic()
    if _cache is not None and _cache[1] > ahora:
        return _cache[0]
    try:
        from core.db.models import PlatformKillSwitch
        from core.db.session import get_session

        async with get_session() as session:
            activo = (
                await session.execute(select(PlatformKillSwitch.active).where(PlatformKillSwitch.id == 1))
            ).scalar_one_or_none()
        if activo is None:
            raise LookupError("sin la fila del interruptor global")
    except Exception as exc:
        log.error("kill_switch_lectura_fallida", error=type(exc).__name__)
        _metrica_fallo()
        await _alertar_fallo(type(exc).__name__)
        _cache = (True, ahora + TTL_FALLO_S)
        return True
    _cache = (bool(activo), ahora + TTL_S)
    return bool(activo)


async def motivo_global() -> str | None:
    return MOTIVO_KILL_SWITCH if await global_activo() else None


def invalidar() -> None:
    """Lo llama el servicio después de escribir, para que el mismo proceso vea su cambio al instante."""
    global _cache
    _cache = None


def _metrica_fallo() -> None:
    try:
        from core.observability.metricas_negocio import kill_switch_lectura_fallida

        kill_switch_lectura_fallida()
    except Exception:  # la métrica nunca rompe la lectura
        pass


async def _alertar_fallo(error: str) -> None:
    try:
        from core.shared.alerts import send_alert_once

        await send_alert_once(
            "kill_switch_lectura",
            "No se pudo leer el interruptor global",
            f"Se trata como activo (falla cerrado): no corren turnos ni envíos automáticos. Error: {error}",
            severity="critical",
            window_s=300,
        )
    except Exception:  # sin Redis/Slack la lectura igual falla cerrado
        pass
