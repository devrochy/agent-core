"""Vigencia del equipo en cada petición y en el refresh (ADR-0174 §3, HU-ACC-02).

El token de campo del gestor lleva `did` (la fila de `account_devices` con la que entró). Bloquear el equipo
corta la sesión abierta porque cada petición vuelve a mirar esa fila, con una caché de 60 s en Redis que
se invalida al escribir (`invalidar`, llamada desde las transiciones del servicio). Si Redis no responde se
lee la base: nunca se falla abierto. El límite real de revocación es el TTL de la caché (< 60 s).

El core no importa del pack (invariante 5): el pack llama a estas funciones desde su validador de tokens.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select

from core.access import policy
from core.access.devices import ACTIVO, BLOQUEADO, AccesoError, _equipo_bloqueado
from core.db.models import AccountDevice
from core.shared.logger import get_logger

log = get_logger(__name__)

TTL_S = 60
MENSAJES: dict[str, str] = {
    "dispositivo_pendiente": "Este equipo está pendiente de aprobación.",
    "dispositivo_bloqueado": "Este equipo está bloqueado.",
    "dispositivo_no_autorizado": "Este equipo ya no está autorizado para esta cuenta.",
    "dispositivo_requerido": "No se pudo identificar este equipo.",
}


def _clave_equipo(tenant_id: uuid.UUID, row_id: uuid.UUID) -> str:
    return f"acceso:equipo:{tenant_id}:{row_id}"


def _clave_cuenta(tenant_id: uuid.UUID, admin_id: uuid.UUID, rol: str) -> str:
    return f"acceso:cuenta:{tenant_id}:{admin_id}:{rol}"


def _clave_peticion(tenant_id: uuid.UUID, admin_id: uuid.UUID, device_id: str) -> str:
    # El id del equipo no va en claro en Redis: se identifica por su huella.
    huella = hashlib.sha256(device_id.encode()).hexdigest()[:24]
    return f"acceso:req:{tenant_id}:{admin_id}:{huella}"


def _redis() -> Any:
    from core.memory.working import get_redis

    return get_redis()


async def _leer(clave: str) -> Any | None:
    try:
        crudo = await _redis().get(clave)
    except Exception:
        log.warning("access_cache_no_disponible", exc_info=True)
        return None  # a la base: el fallo de Redis nunca abre el acceso
    try:
        return json.loads(crudo) if crudo else None
    except ValueError:
        log.warning("access_cache_corrupta", clave=clave)
        return None  # valor ilegible: se trata como caché fría


async def _escribir(clave: str, valor: Any) -> None:
    try:
        await _redis().set(clave, json.dumps(valor), ex=TTL_S)
    except Exception:
        log.warning("access_cache_no_disponible", exc_info=True)


async def invalidar(
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    *row_ids: uuid.UUID,
    device_id: str | None = None,
) -> None:
    """Borra de la caché el estado de estos equipos y el de la cuenta (todos sus roles). Un fallo de Redis
    no impide escribir: el TTL acota la revocación."""
    claves = [
        *(_clave_cuenta(tenant_id, admin_id, r) for r in ("owner", "gestor", "colaborador")),
        *(_clave_equipo(tenant_id, r) for r in row_ids),
    ]
    if device_id:
        claves.append(_clave_peticion(tenant_id, admin_id, device_id))
    try:
        await _redis().delete(*claves)
    except Exception:
        log.warning("access_cache_no_disponible", exc_info=True)


async def motivo_de_denegacion(
    session: Any, tenant_id: uuid.UUID, admin_id: uuid.UUID, did: uuid.UUID
) -> str | None:
    """`None` si el equipo `did` sigue vigente para la cuenta; si no, el `code` del 403 (con caché)."""
    clave = _clave_equipo(tenant_id, did)
    en_cache = await _leer(clave)
    if en_cache is not None and en_cache.get("admin") == str(admin_id):
        return en_cache.get("code")
    fila = (
        await session.execute(
            select(AccountDevice).where(
                AccountDevice.tenant_id == tenant_id,
                AccountDevice.id == did,
                AccountDevice.admin_id == admin_id,
                AccountDevice.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if fila is None:
        code: str | None = "dispositivo_no_autorizado"
    elif fila.estado == BLOQUEADO or (
        fila.device_id and await _equipo_bloqueado(session, tenant_id, fila.device_id)
    ):
        code = "dispositivo_bloqueado"
    elif fila.estado != ACTIVO:
        code = "dispositivo_pendiente"
    else:
        code = None
    await _escribir(clave, {"admin": str(admin_id), "code": code})
    return code


async def cuenta_entra_sin_equipo(
    session: Any, tenant_id: uuid.UUID, admin_id: uuid.UUID, rol: str = "gestor"
) -> bool:
    """¿Se acepta un token sin `did`? Sí si la política del rol es abierta, o si la cuenta ya tiene un
    equipo activo con id (sesiones anteriores a este cambio: se asume el equipo vinculado)."""
    clave = _clave_cuenta(tenant_id, admin_id, rol)
    en_cache = await _leer(clave)
    if en_cache is not None:
        return bool(en_cache.get("ok"))
    abierta = (await policy.leer(session, tenant_id))[rol] == policy.ABIERTO
    ok = abierta or (
        await session.execute(
            select(AccountDevice.id)
            .where(
                AccountDevice.tenant_id == tenant_id,
                AccountDevice.admin_id == admin_id,
                AccountDevice.estado == ACTIVO,
                AccountDevice.device_id.is_not(None),
                AccountDevice.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).first() is not None
    await _escribir(clave, {"ok": ok})
    return ok


def _denegar(code: str) -> AccesoError:
    return AccesoError(403, code, MENSAJES.get(code, MENSAJES["dispositivo_no_autorizado"]))


async def exigir_equipo_vigente(
    session: Any,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    did: str | None,
    *,
    sin_equipo_ok: bool = False,
    rol: str = "gestor",
) -> None:
    """Corta con 403 (`AccesoError`) si el equipo del token ya no vale. `sin_equipo_ok` deja pasar los
    tokens sin `did` en las rutas por las que un equipo pide vincularse."""
    if did:
        try:
            row_id = uuid.UUID(str(did))
        except ValueError:
            raise _denegar("dispositivo_no_autorizado") from None
        code = await motivo_de_denegacion(session, tenant_id, admin_id, row_id)
        if code is not None:
            raise _denegar(code)
        return
    if sin_equipo_ok or await cuenta_entra_sin_equipo(session, tenant_id, admin_id, rol):
        return
    raise _denegar("dispositivo_requerido")


async def evaluar_rapido(
    session: Any, tenant_id: uuid.UUID, admin_id: uuid.UUID, device_id: str
) -> str | None:
    """Decisión de solo lectura para la dependencia del principal (HU-ACC-03): `None` si la petición con
    `X-Device-Id` pasa, o el `code` del 403. No crea filas ni eventos (eso es `POST /access/device-session`).
    Una lectura de Redis por petición; con la caché fría, una consulta indexada por (tenant, cuenta, equipo)."""
    from core.access.devices import _fila
    from core.access.roles import rol_de_acceso

    clave = _clave_peticion(tenant_id, admin_id, device_id)
    en_cache = await _leer(clave)
    if en_cache is not None:
        return en_cache.get("code")
    rol = await rol_de_acceso(session, tenant_id, admin_id)
    if rol is None or rol == "master":  # ajeno al negocio: lo decide otra capa; master: nunca se bloquea
        code: str | None = None
    else:
        restringido = (await policy.leer(session, tenant_id))[rol] == policy.RESTRINGIDO
        fila = await _fila(session, tenant_id, admin_id, device_id)
        if (fila is not None and fila.estado == BLOQUEADO) or await _equipo_bloqueado(
            session, tenant_id, device_id
        ):
            code = "dispositivo_bloqueado"
        elif fila is not None and fila.estado == ACTIVO:
            code = None
        else:  # sin fila o pendiente: solo estorba en política restringida
            code = "dispositivo_pendiente" if restringido else None
    await _escribir(clave, {"code": code})
    return code
