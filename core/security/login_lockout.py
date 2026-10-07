"""Bloqueo progresivo de inicio de sesión (HU-ARCH-04, D-SPEC-01-3).

Regla: 5 intentos fallidos del mismo identificador (o de la misma IP) en 10
minutos → rechazo de 15 minutos con mensaje neutro. El estado vive en Redis
con TTL (se autolimpia); si Redis no responde, el login NO se bloquea
(fail-open explícito: un Redis caído no puede tumbar el acceso legítimo, y
esta capa es complementaria a la revocación de sesión).

Claves:
  auth:fail:<scope>:id:<identificador>   contador de fallos (TTL 10 min)
  auth:fail:<scope>:ip:<ip>              contador de fallos por IP (TTL 10 min)
  auth:block:<scope>:id:<identificador>  bloqueo activo (TTL 15 min)
  auth:block:<scope>:ip:<ip>             bloqueo activo por IP (TTL 15 min)

El `scope` aísla dominios de login: `admin` para el panel/API y
`gestor:<slug>` para la API de campo, de modo que los fallos de un negocio
nunca bloquean el mismo usuario en otro (aislamiento por negocio).
"""

from __future__ import annotations

from core.shared.logger import get_logger

log = get_logger(__name__)

FAIL_PREFIX = "auth:fail:"
BLOCK_PREFIX = "auth:block:"
WINDOW_S = 600
THRESHOLD = 5
BLOCK_S = 900


def _redis():  # type: ignore[no-untyped-def]
    from core.memory.working import get_redis

    return get_redis()


def _fail_id_key(scope: str, identifier: str) -> str:
    return f"{FAIL_PREFIX}{scope}:id:{identifier}"


def _fail_ip_key(scope: str, ip: str) -> str:
    return f"{FAIL_PREFIX}{scope}:ip:{ip}"


def _block_id_key(scope: str, identifier: str) -> str:
    return f"{BLOCK_PREFIX}{scope}:id:{identifier}"


def _block_ip_key(scope: str, ip: str) -> str:
    return f"{BLOCK_PREFIX}{scope}:ip:{ip}"


async def _incr_with_ttl(key: str, ttl_s: int) -> int:
    """INCR que fija el TTL solo al crear la clave (ventana corrediza)."""
    r = _redis()
    total = int(await r.incr(key))
    if total == 1:
        await r.expire(key, ttl_s)
    return total


async def is_blocked(scope: str, identifier: str | None, ip: str | None) -> bool:
    """True si hay un bloqueo activo para el identificador o la IP."""
    try:
        r = _redis()
        keys = []
        if identifier:
            keys.append(_block_id_key(scope, identifier))
        if ip:
            keys.append(_block_ip_key(scope, ip))
        for key in keys:
            if await r.get(key):
                return True
        return False
    except Exception:
        log.warning("login_lockout_redis_error", scope=scope, op="is_blocked", exc_info=True)
        return False


async def register_failure(scope: str, identifier: str | None, ip: str | None) -> bool:
    """Cuenta un intento fallido. True si este fallo disparó el bloqueo."""
    try:
        blocked_now = False
        if identifier:
            total = await _incr_with_ttl(_fail_id_key(scope, identifier), WINDOW_S)
            if total >= THRESHOLD:
                r = _redis()
                await r.set(_block_id_key(scope, identifier), "1", ex=BLOCK_S)
                blocked_now = True
        if ip:
            total = await _incr_with_ttl(_fail_ip_key(scope, ip), WINDOW_S)
            if total >= THRESHOLD:
                r = _redis()
                await r.set(_block_ip_key(scope, ip), "1", ex=BLOCK_S)
                blocked_now = True
        if blocked_now:
            log.warning(
                "auth_login_blocked",
                scope=scope,
                identifier=identifier or "",
                ip=ip or "",
                bloqueo_s=BLOCK_S,
            )
        return blocked_now
    except Exception:
        log.warning("login_lockout_redis_error", scope=scope, op="register_failure", exc_info=True)
        return False


async def register_success(scope: str, identifier: str | None, ip: str | None) -> None:
    """Un login exitoso limpia los fallos acumulados del identificador y de la IP."""
    try:
        r = _redis()
        keys = []
        if identifier:
            keys.append(_fail_id_key(scope, identifier))
        if ip:
            keys.append(_fail_ip_key(scope, ip))
        if keys:
            await r.delete(*keys)
    except Exception:
        log.warning("login_lockout_redis_error", scope=scope, op="register_success", exc_info=True)


def client_ip(request) -> str:
    """IP del cliente detrás del proxy (Cloudflare/BFF), o la del socket."""
    try:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return str(xff.split(",")[0]).strip() or "desconocida"
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return str(cf).strip()
        host = request.client.host if request.client else None
        return str(host or "desconocida")
    except Exception:
        return "desconocida"
