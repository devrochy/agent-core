"""Registro de eventos de acceso (`access_events`, ADR-0174 §4).

`registrar_evento` agrega la fila a la sesión del llamador: el evento nace en la misma
transacción que la decisión que lo provoca (si esta falla, el evento tampoco queda).
El `detalle` nunca debe llevar contraseñas ni tokens; el `device_id` completo no se loguea.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.db.models import AccessEvent, Admin
from core.shared.logger import get_logger

log = get_logger(__name__)

SEVERIDADES: tuple[str, ...] = ("info", "aviso", "critico")
CANALES: tuple[str, ...] = ("campo", "panel_apk", "campo_v1")

# Tipo → severidad por defecto (ADR-0174 §4). Los tipos son los del CHECK de la migración 0113.
SEVERIDAD_POR_TIPO: dict[str, str] = {
    "login_fallido": "aviso",
    "login_bloqueado": "critico",
    "dispositivo_nuevo": "info",
    "dispositivo_pendiente": "aviso",
    "dispositivo_aprobado": "info",
    "dispositivo_bloqueado_usado": "critico",
    "acceso_denegado_politica": "aviso",
    "dispositivo_sin_id": "aviso",
    "dispositivo_compartido": "info",
    "politica_cambiada": "aviso",
    "acceso_master": "info",
    "legado_pack": "info",
}
TIPOS: tuple[str, ...] = tuple(SEVERIDAD_POR_TIPO)


def sufijo(device_id: str | None) -> str | None:
    """Últimos 6 caracteres del equipo: lo único que se muestra en listados y se escribe en logs."""
    return device_id[-6:] if device_id else None


def registrar_evento(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    tipo: str,
    severidad: str | None = None,
    admin_id: uuid.UUID | None = None,
    device_row_id: uuid.UUID | None = None,
    device_id: str | None = None,
    rol: str | None = None,
    canal: str = "campo",
    ip: str | None = None,
    detalle: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AccessEvent:
    if tipo not in SEVERIDAD_POR_TIPO:
        raise ValueError(f"tipo de evento de acceso desconocido: {tipo}")
    severidad = severidad or SEVERIDAD_POR_TIPO[tipo]
    if severidad not in SEVERIDADES:
        raise ValueError(f"severidad desconocida: {severidad}")
    if canal not in CANALES:
        raise ValueError(f"canal desconocido: {canal}")
    evento = AccessEvent(
        tenant_id=tenant_id,
        admin_id=admin_id,
        device_row_id=device_row_id,
        device_id=device_id,
        rol=rol,
        canal=canal,
        tipo=tipo,
        severidad=severidad,
        ip=ip,
        detalle=detalle,
        request_id=request_id,
    )
    session.add(evento)
    log.info(
        "access_event",
        tenant_id=str(tenant_id),
        admin_id=str(admin_id) if admin_id else None,
        tipo=tipo,
        severidad=severidad,
        canal=canal,
        rol=rol,
        device=sufijo(device_id),
    )
    return evento


# --------------------------------------------------------------------- dedupe y fan-out (HU-ACC-06)
DEDUPE_S = 300  # un mismo evento repetido dentro de 5 min se cuenta, no se vuelve a escribir
MAX_NEGOCIOS_POR_FALLO = 10  # tope del fan-out: una cuenta con muchos negocios no multiplica filas sin límite
_DEDUPE = "acceso:dedupe:"
_SUPRIMIDOS_TTL_S = 3600


async def _abrir_ventana(clave: str, ventana_s: int) -> tuple[bool, int]:
    """`(emitir, suprimidos_antes)`. La primera vez de la ventana emite y devuelve cuántas repeticiones se
    callaron en la ventana anterior; dentro de la ventana solo cuenta. Si Redis no responde, emite (es mejor
    un evento de más que perder uno de seguridad)."""
    try:
        from core.memory.working import get_redis

        r = get_redis()
        if await r.set(f"{_DEDUPE}{clave}", "1", nx=True, ex=ventana_s):
            previo = await r.get(f"{_DEDUPE}{clave}:n")
            if previo:
                await r.delete(f"{_DEDUPE}{clave}:n")
            return True, int(previo or 0)
        total = int(await r.incr(f"{_DEDUPE}{clave}:n"))
        if total == 1:
            await r.expire(f"{_DEDUPE}{clave}:n", _SUPRIMIDOS_TTL_S)
        return False, total
    except Exception:
        log.warning("access_event_dedupe_redis_error", exc_info=True)
        return True, 0


async def registrar_evento_unico(
    session: Any, *, ventana_s: int = DEDUPE_S, **kw: Any
) -> AccessEvent | None:
    """Como `registrar_evento`, pero un evento igual (negocio, cuenta, equipo, tipo y canal) dentro de la
    ventana no se repite: devuelve `None`. La tabla es append-only, así que las repeticiones calladas se
    anotan como `repeticiones_previas` en el siguiente evento que sí se escribe."""
    clave = ":".join(
        str(kw.get(k) or "-") for k in ("tenant_id", "admin_id", "device_id", "tipo", "canal")
    )
    emitir, suprimidos = await _abrir_ventana(clave, ventana_s)
    if not emitir:
        return None
    if suprimidos:
        kw["detalle"] = {**(kw.get("detalle") or {}), "repeticiones_previas": suprimidos}
    return registrar_evento(session, **kw)


async def registrar_fallo_de_login(
    identificador: str,
    ip: str | None,
    *,
    canal: str,
    bloqueado: bool = False,
    tenant_id: uuid.UUID | None = None,
    request_id: str | None = None,
) -> int:
    """Deja `login_fallido` (y `login_bloqueado` si este fallo disparó el bloqueo) en los negocios de la
    cuenta que intentaron abrir: una fila por negocio, hasta `MAX_NEGOCIOS_POR_FALLO`, o solo en `tenant_id`
    si el login era de un negocio concreto. Devuelve cuántos negocios recibieron algún evento.

    Un identificador que no es de ninguna cuenta no escribe filas (sería un contador que cualquiera
    puede inflar): solo un log. Nunca lanza: el rastro no puede tumbar ni cambiar la respuesta del login.
    El `detalle` no lleva el identificador: en un campo de usuario a veces se escribe la contraseña."""
    try:
        from core.db.session import get_session
        from core.shared.tenant import admin_tenants, tenant_scope

        ident = identificador.strip()
        async with get_session() as session:
            admin_id = (
                await session.execute(
                    select(Admin.id).where(
                        (Admin.email == ident.lower()) | (Admin.username == ident),
                        Admin.active.is_(True),
                    )
                )
            ).scalar_one_or_none()
        if admin_id is None:
            log.info("login_fallido_sin_cuenta", canal=canal, ip=ip)
            return 0
        destinos = [
            (tid, rol) for tid, _nombre, rol in await admin_tenants(admin_id) if rol != "global"
        ]
        if tenant_id is not None:
            destinos = [d for d in destinos if d[0] == tenant_id]
        if len(destinos) > MAX_NEGOCIOS_POR_FALLO:
            log.warning("login_fallido_fanout_truncado", total=len(destinos), tope=MAX_NEGOCIOS_POR_FALLO)
            destinos = destinos[:MAX_NEGOCIOS_POR_FALLO]
        tipos = ["login_fallido"] + (["login_bloqueado"] if bloqueado else [])
        for tid, rol in destinos:
            async with tenant_scope(tid):
                async with get_session() as session:
                    for tipo in tipos:
                        await registrar_evento_unico(
                            session,
                            tenant_id=tid,
                            admin_id=admin_id,
                            tipo=tipo,
                            rol=rol if rol in ("owner", "colaborador") else None,
                            canal=canal,
                            ip=ip,
                            request_id=request_id,
                        )
        return len(destinos)
    except Exception:
        log.warning("login_fallido_evento_error", canal=canal, exc_info=True)
        return 0
