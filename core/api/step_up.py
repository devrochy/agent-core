"""Verificación de contraseña para acciones sensibles (step-up).

Se usa al revelar credenciales de una conexión y al eliminarla. La contraseña es la del
usuario logueado; se limita a 5 intentos cada 5 minutos por usuario y propósito, y el
acierto limpia el contador. El rate limit es defensa en profundidad: la verificación real
es `verify_password` contra el hash vigente del admin.
"""

from __future__ import annotations

from core.admin.auth import verify_password
from core.api.deps import Principal
from core.api.errors import ApiError
from core.shared.logger import get_logger

log = get_logger(__name__)

_INTENTOS_MAX = 5
_VENTANA_S = 300


async def confirmar_password(principal: Principal, password: str | None, *, proposito: str) -> None:
    """Exige la contraseña actual del usuario. Lanza 400/403/429 con códigos estables."""
    if not password:
        raise ApiError.bad_request("Escribe tu contraseña para continuar.", "password_required")

    redis = None
    key = f"step_up:{proposito}:{principal.admin.id}"
    try:
        from core.memory.working import get_redis

        redis = get_redis()
        intentos = await redis.incr(key)
        if intentos == 1:
            await redis.expire(key, _VENTANA_S)
        if intentos > _INTENTOS_MAX:
            raise ApiError(
                429,
                "too_many_attempts",
                "Demasiados intentos. Espera 5 minutos e inténtalo de nuevo.",
            )
    except ApiError:
        raise
    except Exception:
        # Sin Redis el paso de contraseña sigue funcionando; solo se pierde el contador.
        log.warning("step_up_rate_limit_unavailable", proposito=proposito)
        redis = None

    if not verify_password(password, principal.admin.password_hash):
        log.warning(
            "step_up_password_incorrect",
            proposito=proposito,
            admin_id=str(principal.admin.id),
        )
        raise ApiError(403, "password_incorrect", "La contraseña no es correcta.")

    if redis is not None:
        try:
            await redis.delete(key)
        except Exception:
            log.warning("step_up_rate_limit_reset_failed", proposito=proposito)
