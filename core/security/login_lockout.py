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

import ipaddress

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


def _trusted_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    from core.config import get_settings

    redes = []
    for cidr in (get_settings().trusted_proxy_cidrs or "").split(","):
        cidr = cidr.strip()
        if not cidr:
            continue
        try:
            redes.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            log.warning("trusted_proxy_cidr_invalido", cidr=cidr)
    return redes


def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _is_trusted(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, redes) -> bool:
    return any(ip in red for red in redes)


def client_ip(request) -> str:
    """IP del cliente para el bloqueo de login.

    Las cabeceras de proxy solo cuentan si la conexión llega desde un proxy de
    confianza (`TRUSTED_PROXY_CIDRS`): si no, cualquiera podría mandar su propio
    `X-Forwarded-For` para esquivar el bloqueo o bloquear la IP de otro. Detrás de
    proxies, la IP del cliente es la primera NO confiable leyendo `X-Forwarded-For`
    de derecha a izquierda (cada proxy agrega a la derecha lo que vio; lo de la
    izquierda lo pudo escribir el cliente). El BFF del panel reenvía la cadena que
    recibió, para que la API no vea a todos los usuarios con la IP del panel.
    """
    try:
        peer_raw = request.client.host if request.client else None
        peer = _parse_ip(peer_raw)
        if peer is None:
            return str(peer_raw or "desconocida")
        redes = _trusted_networks()
        if not _is_trusted(peer, redes):
            return str(peer)
        xff = request.headers.get("x-forwarded-for") or ""
        for parte in reversed([p for p in xff.split(",") if p.strip()]):
            ip = _parse_ip(parte)
            if ip is None:
                break  # cadena rota: no se sigue leyendo lo que escribió el cliente
            if not _is_trusted(ip, redes):
                return str(ip)
        cf = _parse_ip(request.headers.get("cf-connecting-ip"))
        if cf is not None:
            return str(cf)
        return str(peer)
    except Exception:
        return "desconocida"


QUOTA_PREFIX = "auth:quota:"


async def within_quota(
    scope: str, identifier: str | None, ip: str | None, *, por_identificador: int, por_ip: int, ventana_s: int
) -> bool:
    """Cupo de pedidos (no de fallos) por identificador y por IP en una ventana (HU-SH-06).

    Cuenta cada pedido. False si alguno de los dos pasó su límite. Mismo criterio que el
    bloqueo: si Redis no responde, deja pasar (fail-open)."""
    try:
        dentro = True
        if identifier:
            total = await _incr_with_ttl(f"{QUOTA_PREFIX}{scope}:id:{identifier}", ventana_s)
            dentro = dentro and total <= por_identificador
        if ip:
            total = await _incr_with_ttl(f"{QUOTA_PREFIX}{scope}:ip:{ip}", ventana_s)
            dentro = dentro and total <= por_ip
        if not dentro:
            log.warning("auth_quota_excedida", scope=scope, identifier=identifier or "", ip=ip or "")
        return dentro
    except Exception:
        log.warning("login_lockout_redis_error", scope=scope, op="within_quota", exc_info=True)
        return True
