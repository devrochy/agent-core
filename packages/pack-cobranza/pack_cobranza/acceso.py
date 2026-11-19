"""Enganche de la app de campo con los dispositivos de la cuenta del core (ADR-0174, HU-ACC-02).

El vínculo cuenta↔equipo vive en el core (`account_devices`); el pack solo decide cómo se lo cuenta a
cada versión de la app y adopta lo que la migración `pack_cobranza_0018` no haya alcanzado a copiar.
El pack importa del core; el core no importa del pack (invariante 5).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.access.devices import evaluar
from core.access.events import registrar_evento
from core.access.sesion import cuenta_entra_sin_equipo, motivo_de_denegacion
from core.db.models import AccountDevice
from core.shared.logger import get_logger
from pack_cobranza.models import Device, Gestor

log = get_logger(__name__)

HEADER_EQUIPO = "x-device-id"
_MAX_EQUIPO = 128  # el id lo manda el cliente: se acota antes de guardarlo (tabla y eventos)
MAX_CONTACTO = 40

MENSAJES: dict[str, str] = {
    "dispositivo_pendiente": "Este equipo está pendiente de aprobación.",
    "dispositivo_bloqueado": "Este equipo está bloqueado.",
    "dispositivo_no_autorizado": "dispositivo no autorizado",
    "dispositivo_requerido": "No se pudo identificar este equipo.",
}


def equipo_de(request: Request, imei_del_cuerpo: str | None = None) -> tuple[str | None, bool]:
    """`(device_id, llegó_por_header)`. La app nueva manda `X-Device-Id` en cada petición; la vieja solo
    `imei` en el cuerpo (un release de convivencia, ADR-0174 §5). El header manda si están los dos."""
    desde_header = (request.headers.get(HEADER_EQUIPO) or "").strip()[:_MAX_EQUIPO]
    if desde_header:
        return desde_header, True
    desde_cuerpo = (imei_del_cuerpo or "").strip()[:_MAX_EQUIPO]
    return (desde_cuerpo or None), False


async def adoptar_legado(session: Any, tenant_id: uuid.UUID, gestor: Gestor) -> int:
    """Copia al core los dispositivos del pack de este gestor que aún no están allí (red de seguridad de
    `pack_cobranza_0018`: esa migración no aborta las de otros packs, así que puede no haber corrido).

    Idempotente: el único parcial por `legacy_pack_device_id` hace que una segunda llamada no cree nada.
    Misma regla que la migración (estado conservado, sin `imei` → `legacy`)."""
    if gestor.admin_id is None or gestor.deleted_at is not None:
        return 0
    por_adoptar = (
        (
            await session.execute(
                select(Device).where(
                    Device.tenant_id == tenant_id,
                    Device.gestor_id == gestor.id,
                    Device.deleted_at.is_(None),
                    ~exists().where(
                        AccountDevice.tenant_id == tenant_id,
                        AccountDevice.legacy_pack_device_id == Device.id,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    adoptados = 0
    for d in por_adoptar:
        imei = (d.imei or "").strip() or None
        estado = d.estado if d.estado in ("activo", "bloqueado", "pendiente") else "pendiente"
        ahora = datetime.now(UTC)
        nuevo_id = (
            await session.execute(
                pg_insert(AccountDevice)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    admin_id=gestor.admin_id,
                    device_id=imei,
                    plataforma="android",
                    estado=estado,
                    origen="migrado_pack",
                    rol_al_registrar="gestor",
                    legacy=imei is None,
                    legacy_pack_device_id=d.id,
                    aprobado_at=(d.fecha_vinculacion or d.created_at)
                    if estado == "activo"
                    else None,
                    bloqueado_at=ahora if estado == "bloqueado" else None,
                    created_at=d.created_at,
                )
                .on_conflict_do_nothing()
                .returning(AccountDevice.id)
            )
        ).scalar_one_or_none()
        if nuevo_id is None:
            continue  # ya existía (el mismo equipo ya se registró en el core): nada que adoptar
        adoptados += 1
        registrar_evento(
            session,
            tenant_id=tenant_id,
            tipo="legado_pack",
            admin_id=gestor.admin_id,
            device_row_id=nuevo_id,
            device_id=imei,
            rol="gestor",
            detalle={"por": "adopcion_perezosa", "pack_device_id": str(d.id)},
        )
    if adoptados:
        log.warning(
            "access_adopcion_perezosa",
            tenant_id=str(tenant_id),
            gestor_id=str(gestor.id),
            n=adoptados,
        )
    return adoptados


def respuesta_403(code: str, estado: str | None = None) -> JSONResponse:
    cuerpo = {"error": MENSAJES.get(code, MENSAJES["dispositivo_no_autorizado"]), "code": code}
    if estado:
        cuerpo["estado"] = estado
    return JSONResponse(cuerpo, status_code=403)


async def equipo_del_refresh(
    request: Request,
    session: Any,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID | None,
    rol: str,
    did_token: str | None,
) -> uuid.UUID | JSONResponse | None:
    """Equipo con el que se renueva la sesión del gestor (ADR-0174): el `did` nuevo, `None` si la política
    es abierta y no hay equipo, o el 403 con `code`. Revalida: bloquear el equipo corta también el refresh.

    - Con `X-Device-Id` se vuelve a decidir sobre ese equipo (puede ser otro que el del token).
    - Sin header, manda el `did` del token (caché de 60 s, invalidada al escribir).
    - Sin ninguno de los dos (token anterior a este cambio o app vieja sin equipo) se hereda el equipo
      activo más reciente de la cuenta si la política lo permite; si no, 403 `dispositivo_requerido`."""
    if admin_id is None:
        return None
    device_id, via_header = equipo_de(request)
    if via_header:
        decision = await evaluar(
            session,
            tenant_id=tenant_id,
            admin_id=admin_id,
            rol=rol,
            device_id=device_id,
            ip=request.client.host if request.client else None,
        )
        if not decision.permitido:
            return respuesta_403(decision.code or "dispositivo_no_autorizado", decision.estado)
        return decision.device_row_id
    if did_token:
        try:
            row_id = uuid.UUID(str(did_token))
        except ValueError:
            return respuesta_403("dispositivo_no_autorizado")
        code = await motivo_de_denegacion(session, tenant_id, admin_id, row_id)
        return respuesta_403(code) if code else row_id
    if not await cuenta_entra_sin_equipo(session, tenant_id, admin_id, rol):
        return respuesta_403("dispositivo_requerido")
    return (
        await session.execute(
            select(AccountDevice.id)
            .where(
                AccountDevice.tenant_id == tenant_id,
                AccountDevice.admin_id == admin_id,
                AccountDevice.estado == "activo",
                AccountDevice.device_id.is_not(None),
                AccountDevice.deleted_at.is_(None),
            )
            .order_by(AccountDevice.ultimo_uso_at.desc().nulls_last())
            .limit(1)
        )
    ).scalar_one_or_none()
