"""Dispositivos de la cuenta, eventos de acceso y política por negocio (`/api/v1/access`, ADR-0174).

Rutas del core para todos los roles (no del pack de cobranza): el panel administra los equipos de
las cuentas del negocio, ve el feed de eventos de acceso y cambia la política (abierta o restringida
por rol). Esta HU (ACC-01) NO cambia el login: el enforcement llega con HU-ACC-02/03.

Permisos: `devices` (lectura/escritura) y `access_alerts` (lectura) son secciones del RBAC, opt-in
para colaboradores. La política la cambia solo el dueño del negocio o el master. Los equipos de un
dueño o del master solo los administra un dueño o el master.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select

from core.access import devices as svc
from core.access import policy as pol
from core.access.events import registrar_evento, sufijo
from core.access.roles import rol_de_acceso
from core.admin.permissions import effective_access
from core.api.deps import (
    Principal,
    es_owner_o_master,
    get_principal,
    require,
    require_owner_or_master,
)
from core.api.errors import ApiError
from core.db.models import AccessEvent, AccountDevice, Admin, TenantUser
from core.db.session import get_session
from core.shared.audit import record_audit

router = APIRouter(prefix="/access", tags=["access"])

Estado = Literal["pendiente", "activo", "bloqueado"]
Plataforma = Literal["android", "ios"]
Valor = Literal["abierto", "restringido"]
Origen = Literal["autoregistro", "politica_abierta", "alta_manual", "migrado_pack"]
Severidad = Literal["info", "aviso", "critico"]
Canal = Literal["campo", "panel_apk", "campo_v1"]
RolCuenta = Literal["master", "owner", "colaborador"]
RolAcceso = Literal["owner", "colaborador", "gestor"]
TipoEvento = Literal[
    "login_fallido",
    "login_bloqueado",
    "dispositivo_nuevo",
    "dispositivo_pendiente",
    "dispositivo_aprobado",
    "dispositivo_bloqueado_usado",
    "acceso_denegado_politica",
    "dispositivo_sin_id",
    "dispositivo_compartido",
    "politica_cambiada",
    "acceso_master",
    "legado_pack",
]


# ------------------------------------------------------------------ esquemas
class AccessAccountRef(BaseModel):
    id: uuid.UUID
    nombre: str | None
    email: str
    # El gestor es `colaborador` aquí: su rol de acceso va en `rol_al_registrar` del dispositivo.
    rol: RolCuenta | None


class AccessDeviceOut(BaseModel):
    id: uuid.UUID
    cuenta: AccessAccountRef | None
    device_id_corto: str | None
    device_label: str | None
    plataforma: Plataforma
    estado: Estado
    origen: Origen
    rol_al_registrar: RolAcceso | None
    bloqueo_equipo: bool
    legacy: bool
    ultimo_uso_at: datetime | None
    ultima_ip: str | None
    aprobado_at: datetime | None
    bloqueado_at: datetime | None
    motivo: str | None
    created_at: datetime


class AccessDeviceDetail(AccessDeviceOut):
    """El detalle muestra el identificador completo: hay que compararlo al aprobar."""

    device_id: str | None


class AccessDevicePage(BaseModel):
    items: list[AccessDeviceOut]
    next_cursor: str | None


class AccessDeviceCreate(BaseModel):
    admin_id: uuid.UUID
    device_id: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=120)
    plataforma: Plataforma = "android"


class AccessDeviceTransition(BaseModel):
    esperado: Estado


class AccessDeviceBlock(BaseModel):
    esperado: Estado = "activo"
    motivo: str | None = Field(default=None, max_length=300)
    alcance: Literal["cuenta", "equipo"] = "cuenta"


class AccessEventOut(BaseModel):
    id: uuid.UUID
    tipo: TipoEvento
    severidad: Severidad
    canal: Canal
    rol: RolAcceso | None
    cuenta: AccessAccountRef | None
    device_id_corto: str | None
    ip: str | None
    detalle: dict[str, Any] | None
    occurred_at: datetime


class AccessEventPage(BaseModel):
    items: list[AccessEventOut]
    next_cursor: str | None


class AccessPolicyOut(BaseModel):
    owner: Valor
    colaborador: Valor
    gestor: Valor


class AccessPolicyUpdate(BaseModel):
    owner: Valor | None = None
    colaborador: Valor | None = None
    gestor: Valor | None = None
    confirmar: bool = False


class DeviceSessionIn(BaseModel):
    """Check-in del equipo de la APK (camelCase: es el contrato de la app, HU-ACC-03)."""

    deviceId: str = Field(min_length=1, max_length=128)
    label: str | None = Field(default=None, max_length=120)
    plataforma: Literal["android", "ios"] = "android"
    appVersion: str | None = Field(default=None, max_length=40)


class DeviceSessionOut(BaseModel):
    estado: Literal["activo", "pendiente", "bloqueado", "none"]
    politica: Literal["abierto", "restringido", "plataforma"]
    permitido: bool
    dispositivoId: uuid.UUID | None = None
    requiereVinculacion: bool


# ------------------------------------------------------------------ utilidades
def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError(400, "sin_negocio", "Selecciona un negocio (X-Tenant-Id).")
    return principal.tenant_id


@asynccontextmanager
async def _sesion() -> AsyncIterator[Any]:
    """Sesión del negocio activo; traduce los errores de dominio al sobre de la API."""
    try:
        async with get_session() as session:
            yield session
    except svc.AccesoError as exc:
        raise ApiError(exc.status, exc.code, exc.message, details=exc.details) from exc


def _cursor(ts: datetime, fila_id: uuid.UUID) -> str:
    """Opaco y seguro en una URL (el `+` de la zona horaria se perdería en una query)."""
    return base64.urlsafe_b64encode(f"{ts.isoformat()}|{fila_id}".encode()).decode()


def _parse_cursor(raw: str) -> tuple[datetime, uuid.UUID]:
    try:
        ts, _, fid = base64.urlsafe_b64decode(raw.encode()).decode().partition("|")
        return datetime.fromisoformat(ts), uuid.UUID(fid)
    except ValueError as exc:
        raise ApiError(400, "cursor_invalido", "El cursor de paginación no es válido.") from exc


async def _cuentas(
    session: Any, tenant_id: uuid.UUID, ids: set[uuid.UUID]
) -> dict[uuid.UUID, AccessAccountRef]:
    """Nombre, email y rol de las cuentas; solo miembros de ESTE negocio o admins de plataforma.

    `access_events.admin_id` no tiene FK ni valida pertenencia (un `login_fallido` puede referir una cuenta
    que existe pero no es de este negocio): esa cuenta responde `None` en vez de enseñar su email.
    """
    if not ids:
        return {}
    roles = dict(
        (
            await session.execute(
                select(TenantUser.admin_id, TenantUser.role).where(
                    TenantUser.tenant_id == tenant_id, TenantUser.admin_id.in_(ids)
                )
            )
        ).all()
    )
    admins = (
        await session.execute(
            select(Admin.id, Admin.nombre, Admin.email, Admin.role).where(Admin.id.in_(ids))
        )
    ).all()
    return {
        a.id: AccessAccountRef(
            id=a.id,
            nombre=a.nombre,
            email=a.email,
            rol="master" if a.role == "master" else roles[a.id],
        )
        for a in admins
        if a.role == "master" or a.id in roles
    }


def _device_out(d: AccountDevice, cuentas: dict[uuid.UUID, AccessAccountRef]) -> AccessDeviceOut:
    return AccessDeviceOut(
        id=d.id,
        cuenta=cuentas.get(d.admin_id),
        device_id_corto=sufijo(d.device_id),
        device_label=d.device_label,
        plataforma=d.plataforma,
        estado=d.estado,
        origen=d.origen,
        rol_al_registrar=d.rol_al_registrar,
        bloqueo_equipo=d.bloqueo_equipo,
        legacy=d.legacy,
        ultimo_uso_at=d.ultimo_uso_at,
        ultima_ip=d.ultima_ip,
        aprobado_at=d.aprobado_at,
        bloqueado_at=d.bloqueado_at,
        motivo=d.motivo,
        created_at=d.created_at,
    )


async def _una(session: Any, tenant_id: uuid.UUID, d: AccountDevice) -> AccessDeviceOut:
    return _device_out(d, await _cuentas(session, tenant_id, {d.admin_id}))


async def _device_id_visible(
    session: Any, principal: Principal, tenant_id: uuid.UUID, fila: AccountDevice
) -> str | None:
    """El identificador completo permite suplantar el equipo ante el enforcement (viaja en `X-Device-Id`):
    lo ve el dueño o el master, y quien administra equipos (`devices:write`) salvo los del dueño o el master.
    Quien solo lee ve los últimos 6 caracteres."""
    if es_owner_o_master(principal):
        return fila.device_id
    if await effective_access(principal.admin, "devices", tenant_id) != "write":
        return None
    if await svc.rol_de_cuenta(session, tenant_id, fila.admin_id) in ("owner", "master"):
        return None
    return fila.device_id


def _actor(principal: Principal) -> str:
    return f"admin:{principal.admin.email}"


@router.post("/device-session", response_model=DeviceSessionOut)
async def device_session(
    payload: DeviceSessionIn,
    principal: Annotated[Principal, Depends(get_principal)],
    request: Request,
) -> DeviceSessionOut:
    """Registra (o consulta) el equipo de la APK para la cuenta que llama y dice si puede operar.

    Sirve a cualquier rol del negocio, pero solo para SU propio `admin_id` (el del token): nadie registra un
    equipo en nombre de otro. El equipo pendiente o bloqueado también puede llamarla (ruta exenta de la
    dependencia del principal): así consulta su estado y puede cerrar sesión. Con la política abierta el
    alta es automática; con la restringida queda `pendiente` hasta que un admin lo apruebe."""
    tenant_id = _tenant(principal)
    async with _sesion() as session:
        rol = await rol_de_acceso(session, tenant_id, principal.admin.id)
        if rol is None:
            raise ApiError(403, "fuera_del_negocio", "Tu cuenta no pertenece a este negocio.")
        decision = await svc.evaluar(
            session,
            tenant_id=tenant_id,
            admin_id=principal.admin.id,
            rol="owner" if rol == "master" else rol,  # el master no tiene política: ver es_master_plataforma
            device_id=payload.deviceId,
            label=payload.label,
            plataforma=payload.plataforma,
            canal="panel_apk",
            ip=request.client.host if request.client else None,
            es_master_plataforma=rol == "master",
            detalle_alta={"appVersion": payload.appVersion} if payload.appVersion else None,
        )
    return DeviceSessionOut(
        estado=decision.estado,  # type: ignore[arg-type]
        politica=decision.politica,  # type: ignore[arg-type]
        permitido=decision.permitido,
        dispositivoId=decision.device_row_id,
        requiereVinculacion=not decision.permitido and decision.estado == "pendiente",
    )


# ------------------------------------------------------------------ dispositivos
@router.get("/devices", response_model=AccessDevicePage)
async def list_devices(
    principal: Annotated[Principal, Depends(require("devices"))],
    estado: Annotated[Estado | None, Query()] = None,
    admin_id: Annotated[uuid.UUID | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AccessDevicePage:
    tenant_id = _tenant(principal)
    async with _sesion() as session:
        consulta = select(AccountDevice).where(
            AccountDevice.tenant_id == tenant_id, AccountDevice.deleted_at.is_(None)
        )
        if estado is not None:
            consulta = consulta.where(AccountDevice.estado == estado)
        if admin_id is not None:
            consulta = consulta.where(AccountDevice.admin_id == admin_id)
        if cursor:
            ts, fid = _parse_cursor(cursor)
            consulta = consulta.where(
                or_(
                    AccountDevice.created_at < ts,
                    and_(AccountDevice.created_at == ts, AccountDevice.id < fid),
                )
            )
        filas = (
            (
                await session.execute(
                    consulta.order_by(
                        AccountDevice.created_at.desc(), AccountDevice.id.desc()
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        hay_mas = len(filas) > limit
        filas = filas[:limit]
        cuentas = await _cuentas(session, tenant_id, {f.admin_id for f in filas})
        return AccessDevicePage(
            items=[_device_out(f, cuentas) for f in filas],
            next_cursor=_cursor(filas[-1].created_at, filas[-1].id) if hay_mas and filas else None,
        )


@router.get("/devices/{device_row_id}", response_model=AccessDeviceDetail)
async def get_device(
    device_row_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("devices"))],
) -> AccessDeviceDetail:
    tenant_id = _tenant(principal)
    async with _sesion() as session:
        fila = await svc.cargar(session, tenant_id, device_row_id)
        base = await _una(session, tenant_id, fila)
        return AccessDeviceDetail(
            **base.model_dump(),
            device_id=await _device_id_visible(session, principal, tenant_id, fila),
        )


@router.post("/devices", response_model=AccessDeviceOut, status_code=201)
async def create_device(
    payload: AccessDeviceCreate,
    principal: Annotated[Principal, Depends(require("devices", write=True))],
) -> AccessDeviceOut:
    tenant_id = _tenant(principal)
    async with _sesion() as session:
        fila = await svc.alta_manual(
            session,
            tenant_id=tenant_id,
            admin_id=payload.admin_id,
            device_id=payload.device_id,
            label=payload.label,
            plataforma=payload.plataforma,
            actor_admin_id=principal.admin.id,
            actor_es_owner_o_master=es_owner_o_master(principal),
        )
        out = await _una(session, tenant_id, fila)
    await record_audit(
        _actor(principal),
        "dispositivo_alta_manual",
        input_data={"device_row_id": str(out.id), "admin_id": str(payload.admin_id)},
    )
    return out


@router.post("/devices/{device_row_id}/aprobar", response_model=AccessDeviceOut)
async def approve_device(
    device_row_id: uuid.UUID,
    payload: AccessDeviceTransition,
    principal: Annotated[Principal, Depends(require("devices", write=True))],
) -> AccessDeviceOut:
    tenant_id = _tenant(principal)
    async with _sesion() as session:
        fila = await svc.aprobar(
            session,
            tenant_id=tenant_id,
            device_row_id=device_row_id,
            esperado=payload.esperado,
            actor_admin_id=principal.admin.id,
            actor_es_owner_o_master=es_owner_o_master(principal),
            canal="panel_apk",
        )
        out = await _una(session, tenant_id, fila)
    await record_audit(
        _actor(principal),
        "dispositivo_aprobado",
        input_data={"device_row_id": str(device_row_id), "esperado": payload.esperado},
    )
    return out


@router.post("/devices/{device_row_id}/bloquear", response_model=AccessDeviceOut)
async def block_device(
    device_row_id: uuid.UUID,
    payload: AccessDeviceBlock,
    principal: Annotated[Principal, Depends(require("devices", write=True))],
    x_device_id: Annotated[str | None, Header(alias="X-Device-Id")] = None,
) -> AccessDeviceOut:
    tenant_id = _tenant(principal)
    async with _sesion() as session:
        fila = await svc.bloquear(
            session,
            tenant_id=tenant_id,
            device_row_id=device_row_id,
            esperado=payload.esperado,
            motivo=payload.motivo,
            alcance=payload.alcance,
            actor_admin_id=principal.admin.id,
            actor_es_owner_o_master=es_owner_o_master(principal),
            llamante_device_id=(x_device_id or "").strip() or None,
        )
        out = await _una(session, tenant_id, fila)
    await record_audit(
        _actor(principal),
        "dispositivo_bloqueado",
        input_data={
            "device_row_id": str(device_row_id),
            "alcance": payload.alcance,
            "motivo": payload.motivo,
        },
    )
    return out


@router.post("/devices/{device_row_id}/dar-de-baja", status_code=204)
async def remove_device(
    device_row_id: uuid.UUID,
    payload: AccessDeviceTransition,
    principal: Annotated[Principal, Depends(require("devices", write=True))],
) -> Response:
    tenant_id = _tenant(principal)
    async with _sesion() as session:
        await svc.dar_de_baja(
            session,
            tenant_id=tenant_id,
            device_row_id=device_row_id,
            esperado=payload.esperado,
            actor_es_owner_o_master=es_owner_o_master(principal),
        )
    await record_audit(
        _actor(principal), "dispositivo_baja", input_data={"device_row_id": str(device_row_id)}
    )
    return Response(status_code=204)


# ------------------------------------------------------------------ eventos
@router.get("/events", response_model=AccessEventPage)
async def list_events(
    principal: Annotated[Principal, Depends(require("access_alerts"))],
    severidad: Annotated[Severidad | None, Query()] = None,
    tipo: Annotated[TipoEvento | None, Query()] = None,
    admin_id: Annotated[uuid.UUID | None, Query()] = None,
    desde: Annotated[datetime | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AccessEventPage:
    tenant_id = _tenant(principal)
    async with _sesion() as session:
        consulta = select(AccessEvent).where(
            AccessEvent.tenant_id == tenant_id, AccessEvent.deleted_at.is_(None)
        )
        if severidad is not None:
            consulta = consulta.where(AccessEvent.severidad == severidad)
        if tipo is not None:
            consulta = consulta.where(AccessEvent.tipo == tipo)
        if admin_id is not None:
            consulta = consulta.where(AccessEvent.admin_id == admin_id)
        if desde is not None:
            consulta = consulta.where(AccessEvent.occurred_at >= desde)
        if cursor:
            ts, fid = _parse_cursor(cursor)
            consulta = consulta.where(
                or_(
                    AccessEvent.occurred_at < ts,
                    and_(AccessEvent.occurred_at == ts, AccessEvent.id < fid),
                )
            )
        filas = (
            (
                await session.execute(
                    consulta.order_by(AccessEvent.occurred_at.desc(), AccessEvent.id.desc()).limit(
                        limit + 1
                    )
                )
            )
            .scalars()
            .all()
        )
        hay_mas = len(filas) > limit
        filas = filas[:limit]
        cuentas = await _cuentas(session, tenant_id, {f.admin_id for f in filas if f.admin_id})
        return AccessEventPage(
            items=[
                AccessEventOut(
                    id=f.id,
                    tipo=f.tipo,
                    severidad=f.severidad,
                    canal=f.canal,
                    rol=f.rol,
                    cuenta=cuentas.get(f.admin_id) if f.admin_id else None,
                    device_id_corto=sufijo(f.device_id),
                    ip=f.ip,
                    detalle=f.detalle,
                    occurred_at=f.occurred_at,
                )
                for f in filas
            ],
            next_cursor=_cursor(filas[-1].occurred_at, filas[-1].id) if hay_mas and filas else None,
        )


# ------------------------------------------------------------------ política
@router.get("/policy", response_model=AccessPolicyOut)
async def get_policy(principal: Annotated[Principal, Depends(get_principal)]) -> AccessPolicyOut:
    tenant_id = _tenant(principal)
    async with _sesion() as session:
        return AccessPolicyOut(**await pol.leer(session, tenant_id))


@router.put("/policy", response_model=AccessPolicyOut)
async def update_policy(
    payload: AccessPolicyUpdate,
    principal: Annotated[Principal, Depends(get_principal)],
) -> AccessPolicyOut:
    require_owner_or_master(principal)
    tenant_id = _tenant(principal)
    cambios = {k: v for k, v in payload.model_dump(exclude={"confirmar"}).items() if v is not None}
    async with _sesion() as session:
        actual = await pol.leer(session, tenant_id)
        if (
            cambios.get("owner") == pol.RESTRINGIDO
            and actual["owner"] != pol.RESTRINGIDO
            and not payload.confirmar
        ):
            sin_equipo = await _owners_sin_equipo_activo(session, tenant_id)
            if sin_equipo:
                raise ApiError(
                    409,
                    "owners_sin_dispositivo_activo",
                    "Hay dueños sin un equipo activo: al restringir quedarían fuera de la app. "
                    "Confirma para continuar.",
                    details={"cuentas": sin_equipo},
                )
        antes, despues = await pol.escribir(session, tenant_id, cambios)
        if antes != despues:
            registrar_evento(
                session,
                tenant_id=tenant_id,
                tipo="politica_cambiada",
                admin_id=principal.admin.id,
                canal="panel_apk",
                detalle={"antes": antes, "despues": despues},
            )
    if antes != despues:
        await record_audit(
            _actor(principal),
            "politica_dispositivos_cambiada",
            input_data={"antes": antes, "despues": despues},
        )
    return AccessPolicyOut(**despues)


async def _owners_sin_equipo_activo(session: Any, tenant_id: uuid.UUID) -> list[dict[str, str]]:
    owners = (
        (
            await session.execute(
                select(TenantUser.admin_id).where(
                    TenantUser.tenant_id == tenant_id, TenantUser.role == "owner"
                )
            )
        )
        .scalars()
        .all()
    )
    con_equipo = set(
        (
            await session.execute(
                select(AccountDevice.admin_id).where(
                    AccountDevice.tenant_id == tenant_id,
                    AccountDevice.admin_id.in_(owners),
                    AccountDevice.estado == svc.ACTIVO,
                    AccountDevice.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    faltan = [o for o in owners if o not in con_equipo]
    if not faltan:
        return []
    emails = dict(
        (await session.execute(select(Admin.id, Admin.email).where(Admin.id.in_(faltan)))).all()
    )
    return [{"id": str(o), "email": emails.get(o, "")} for o in faltan]
