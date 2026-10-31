"""API de campo del gestor — contrato de la APK (ADR-0065).

Router separado del `api.py` de campo v1 (ADR-0057). Implementa el contrato exacto
de `solvershub-apk`: mismas rutas, **camelCase** idéntico a `types/gestor.ts` y
**IDs como string** (UUID).

Identidad unificada (ADR-0066): gestores y propietarios son **colaboradores del
core** (`admins` + `tenant_user`); el gestor además tiene su perfil `Gestor`.

F1 (esta tanda): autenticación y perfil.
- `POST /auth/gestor/login` `{tenant, usuario, password}` → `{accessToken, refreshToken, gestor}`.
- `POST /auth/propietario/login` `{tenant, usuario, password}` → `{accessToken, refreshToken, propietario}`.
- `POST /auth/refresh` `{refreshToken}` → `{accessToken, refreshToken}`.
- `PATCH /perfil` `{nombre, apellido}` → `PerfilPublic`.

`tenant` (slug) es una adición obligatoria del contrato: agent-core es multi-tenant y
el login no traía identificador de tenant.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from core.access.devices import AccesoError, evaluar
from core.admin.auth import verify_password
from core.admin.roles import cuenta_habilitada
from core.db.models import AccountDevice, Admin, Tenant, TenantUser
from core.db.session import get_owned, get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from pack_cobranza import acceso
from pack_cobranza.auth import acceso_de_campo, issue_token, pack_secret, token_tenant, verify_token
from pack_cobranza.enlazar import enlazar_cliente, telefono_para_guardar
from pack_cobranza.estadisticas import conteos_cartera
from pack_cobranza.evidencias import EvidenciaError, guardar_archivo, guardar_evidencia
from pack_cobranza.models import (
    Abono,
    AuditoriaCartera,
    Caja,
    CambioCliente,
    Cartera,
    CarteraApertura,
    CarteraConfig,
    CarteraEstadisticasSnapshot,
    CarteraNota,
    CarteraOptimizadaLog,
    Cliente,
    ClienteEvidencia,
    Cuota,
    Gasto,
    GastoEvidencia,
    Gestor,
    GestorPermiso,
    Liquidacion,
    Pago,
    PosicionGestor,
    Prestamo,
    Promesa,
    SyncEvento,
    Visita,
)
from pack_cobranza.permisos import GESTOR_PERMISOS

log = get_logger(__name__)

PLUGIN_ID = "pack_cobranza"
router = APIRouter(prefix="/api/pack/pack_cobranza", tags=[f"{PLUGIN_ID}_gestor"])

_ACCESS_TTL_H = 12
_REFRESH_TTL_H = 720  # 30 días


def _par_tokens(
    secret: str,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    *,
    rol: str,
    gestor_id: uuid.UUID | None = None,
    did: uuid.UUID | None = None,
) -> dict[str, str]:
    base = {"tenant_id": str(tenant_id), "admin_id": str(admin_id), "rol": rol}
    if gestor_id is not None:
        base["gestor_id"] = str(gestor_id)
    if did is not None:  # equipo con el que entró (fila de account_devices): se revalida en cada petición
        base["did"] = str(did)
    return {
        "accessToken": issue_token({**base, "typ": "access"}, secret, ttl_hours=_ACCESS_TTL_H),
        "refreshToken": issue_token({**base, "typ": "refresh"}, secret, ttl_hours=_REFRESH_TTL_H),
    }


def _usuario_de(admin: Admin) -> str:
    return admin.username or admin.email


async def _auth(
    authorization: str | None, *, sin_equipo_ok: bool = False
) -> tuple[uuid.UUID, dict[str, Any]] | None:
    """Token de acceso de campo válido, de una cuenta habilitada, vinculada y de un negocio
    activo: lo decide `acceso_de_campo` (ADR-0134, ADR-0136), el mismo validador de `api.py`.

    Un token de dispositivo (sin `admin_id`, ADR-0057) no trae `rol`: aquí cuenta como
    `gestor` (solo sus carteras y sus permisos), nunca como acceso total."""
    auth = await acceso_de_campo(authorization, sin_equipo_ok=sin_equipo_ok)
    if auth is None:
        return None
    tenant_id, payload = auth
    if not payload.get("admin_id"):
        payload = {**payload, "rol": "gestor"}
    return tenant_id, payload


def _membresia_token(admin_id: uuid.UUID) -> str:
    """Token de membresía (ADR-0081 §5): firmado con `APP_SECRET_KEY`, scope invitaciones."""
    from core.config import get_settings

    return issue_token(
        {"admin_id": str(admin_id), "typ": "membresia"},
        get_settings().app_secret_key,
        ttl_hours=1,
    )


def _admin_de_membresia(token: str) -> uuid.UUID | None:
    from core.config import get_settings

    payload = verify_token(token, get_settings().app_secret_key)
    if payload is None or payload.get("typ") != "membresia":
        return None
    try:
        return uuid.UUID(str(payload.get("admin_id")))
    except (TypeError, ValueError):
        return None


async def _admin_de_token(authorization: str | None) -> uuid.UUID | None:
    """Identidad del admin desde un token de ACCESO de campo (pack) o de membresía.

    El refresh no sirve aquí, y la cuenta debe seguir habilitada (ADR-0136): una cuenta
    desactivada no lista ni acepta invitaciones con un token que le quedó en el teléfono."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    auth = await token_tenant(token)
    admin_uuid: uuid.UUID | None
    if auth is not None:
        if auth[1].get("typ") == "refresh":
            return None
        admin_id = auth[1].get("admin_id")
        try:
            admin_uuid = uuid.UUID(str(admin_id)) if admin_id else None
        except (TypeError, ValueError):
            return None
    else:
        admin_uuid = _admin_de_membresia(token)
    if admin_uuid is None:
        return None
    async with get_session() as session:
        admin = await session.get(Admin, admin_uuid)
    return admin_uuid if cuenta_habilitada(admin) else None


async def _admin_por_credenciales(session, usuario: str, password: str) -> Admin | None:
    admin = (
        await session.execute(
            select(Admin).where((Admin.email == usuario.lower()) | (Admin.username == usuario), Admin.active.is_(True))
        )
    ).scalar_one_or_none()
    if admin is None or not verify_password(password, admin.password_hash):
        return None
    # Tester con el rol apagado (producción, ADR-0134): tampoco entra a la app de campo.
    if not cuenta_habilitada(admin):
        return None
    return admin


def _perfil_admin(admin: Admin) -> dict[str, str]:
    return {
        "id": str(admin.id),
        "usuario": _usuario_de(admin),
        "nombre": _usuario_de(admin),
        "apellido": "",
    }


async def _gestor_de(session, tenant_id: uuid.UUID, admin_id: uuid.UUID) -> Gestor | None:
    """Perfil `Gestor` activo del admin en el tenant, si existe."""
    gestor = (
        await session.execute(
            select(Gestor).where(
                Gestor.tenant_id == tenant_id,
                Gestor.admin_id == admin_id,
                Gestor.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if gestor is None or gestor.estatus != "activo":
        return None
    return gestor


async def _membresias_de_campo(admin: Admin, *, solo: uuid.UUID | None = None) -> list[dict[str, Any]]:
    """Negocios donde el admin accede a la API de campo, con su rol (ADR-0081).

    - `master` (rol `global`: master de plataforma o permiso global) → acceso total.
    - `owner` del tenant → acceso total (`propietario`).
    - colaborador con perfil `Gestor` activo → `gestor` (scopeado a sus carteras).
    Solo tenants activos con `pack_cobranza` habilitado en su AgentSpec.
    """
    from core.agents.spec import enabled_packs, load_active_spec
    from core.shared.tenant import admin_tenants

    filas = await admin_tenants(admin.id)
    if not filas:
        return []
    if solo is not None:
        filas = [f for f in filas if f[0] == solo]
        if not filas:
            return []
    ids = [tid for tid, _n, _r in filas]
    async with get_session() as session:
        activos = {
            t.id: t
            for t in (
                await session.execute(
                    select(Tenant).where(
                        Tenant.id.in_(ids),
                        Tenant.status == "active",
                        Tenant.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        }
    out: list[dict[str, Any]] = []
    for tid, _nombre, role in filas:
        tenant = activos.get(tid)
        if tenant is None:
            continue
        async with tenant_scope(tid):
            spec = await load_active_spec(tid)
            if PLUGIN_ID not in enabled_packs(spec):
                continue
            if role in ("global", "owner"):
                out.append(
                    {
                        "tenantId": str(tid),
                        "slug": tenant.slug,
                        "nombre": tenant.name,
                        "rol": "master" if role == "global" else "propietario",
                        "gestorId": None,
                    }
                )
                continue
            async with get_session() as session:
                gestor = await _gestor_de(session, tid, admin.id)
            if gestor is not None:
                out.append(
                    {
                        "tenantId": str(tid),
                        "slug": tenant.slug,
                        "nombre": tenant.name,
                        "rol": "gestor",
                        "gestorId": str(gestor.id),
                    }
                )
    return out


async def _otro_equipo_activo(session, tenant_id: uuid.UUID, admin_id: uuid.UUID, device_id: str | None) -> bool:
    """¿La cuenta ya tiene un equipo ACTIVO con id distinto de este? (contrato anterior: ese gestor ya
    estaba vinculado a otro teléfono, así que un equipo nuevo se rechaza en vez de pedir vinculación)."""
    consulta = select(AccountDevice.id).where(
        AccountDevice.tenant_id == tenant_id,
        AccountDevice.admin_id == admin_id,
        AccountDevice.estado == "activo",
        AccountDevice.device_id.is_not(None),
        AccountDevice.deleted_at.is_(None),
    )
    if device_id:
        consulta = consulta.where(AccountDevice.device_id != device_id)
    return (await session.execute(consulta.limit(1))).first() is not None


async def _enforcement_dispositivo(
    request: Request, session, tenant_id: uuid.UUID, gestor: Gestor, imei: str, whatsapp: str
) -> tuple[bool, str, uuid.UUID | None] | JSONResponse:
    """Vinculación de dispositivo (ADR-0068, ADR-0174): `(vinculado, estado, did)` o 403.

    La decisión la toma el core (`evaluar`, por cuenta y con la política del negocio). Aquí solo se
    traduce a lo que entiende cada versión de la app: con `X-Device-Id` (app nueva) el 403 lleva `code`;
    sin él (app vieja, solo `imei` en el cuerpo) se conserva el contrato anterior: un equipo por
    vincular recibe los tokens con `requiereVinculacion` en vez de un 403."""
    device_id, via_header = acceso.equipo_de(request, imei)
    if gestor.admin_id is None:  # no ocurre en una membresía de gestor; sin cuenta no hay a quién atar el equipo
        return True, "activo", None
    await acceso.adoptar_legado(session, tenant_id, gestor)
    if not via_header:
        log.info("access_legacy_param", tenant_id=str(tenant_id), tiene_imei=bool(device_id))
    try:
        decision = await evaluar(
            session,
            tenant_id=tenant_id,
            admin_id=gestor.admin_id,
            rol="gestor",
            device_id=device_id,
            canal="campo" if via_header else "campo_v1",
            ip=request.client.host if request.client else None,
            detalle_alta={"contacto": whatsapp[: acceso.MAX_CONTACTO]} if whatsapp else None,
        )
    except AccesoError as exc:
        return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status)
    if decision.permitido:
        return True, "activo", decision.device_row_id

    def _denegar(code: str) -> JSONResponse:
        return acceso.respuesta_403(code, decision.estado)

    code = decision.code or "dispositivo_no_autorizado"
    # Sin equipo identificable (ni header ni imei) en política restringida no hay contrato anterior que
    # conservar: ninguna versión de la app entra así.
    if via_header or code in ("dispositivo_bloqueado", "dispositivo_requerido"):
        return _denegar(code)
    # Contrato anterior (sin header). Un gestor que ya tiene un equipo activo con id no entra por
    # «por vincular»: ni con otro equipo ni sin identificar el suyo (los tokens abren toda la API).
    if await _otro_equipo_activo(session, tenant_id, gestor.admin_id, device_id):
        return _denegar("dispositivo_no_autorizado")
    if code == "dispositivo_pendiente":
        return False, "pendiente", None
    return False, "none", None


async def _enforcement_cuenta(
    request: Request, session, tenant_id: uuid.UUID, admin: Admin, rol: str, imei: str
) -> uuid.UUID | JSONResponse | None:
    """Equipo del propietario (política `owner`) o del master (solo el rastro `acceso_master`), ADR-0174.

    Devuelve el `did` del token, `None` si no hay equipo que atar o el 403 con `code`."""
    device_id, via_header = acceso.equipo_de(request, imei)
    if not via_header:
        log.info("access_legacy_param", tenant_id=str(tenant_id), tiene_imei=bool(device_id))
    try:
        decision = await evaluar(
            session,
            tenant_id=tenant_id,
            admin_id=admin.id,
            rol="owner",
            device_id=device_id,
            canal="campo" if via_header else "campo_v1",
            ip=request.client.host if request.client else None,
            es_master_plataforma=rol == "master",
        )
    except AccesoError as exc:
        return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status)
    if decision.permitido:
        return decision.device_row_id
    return acceso.respuesta_403(decision.code or "dispositivo_no_autorizado", decision.estado)


async def _sesion_de_membresia(
    request: Request, admin: Admin, membresia: dict[str, Any], imei: str, whatsapp: str
) -> JSONResponse:
    """Emite los tokens del negocio elegido (ADR-0081)."""
    tenant_id = uuid.UUID(str(membresia["tenantId"]))
    rol = str(membresia["rol"])
    gestor_id = uuid.UUID(str(membresia["gestorId"])) if membresia.get("gestorId") else None
    async with tenant_scope(tenant_id):
        secret = await pack_secret()
        if not secret:
            return JSONResponse({"error": "pack sin secreto de firma"}, status_code=503)
        vinculado, estado_dispositivo, telefono, did = False, "none", "", None
        async with get_session() as session:
            if rol == "gestor" and gestor_id is not None:
                gestor = await get_owned(session, Gestor, gestor_id)
                if gestor is None or gestor.estatus != "activo":
                    return JSONResponse({"error": "no es gestor de esta cartera"}, status_code=403)
                res = await _enforcement_dispositivo(request, session, tenant_id, gestor, imei, whatsapp)
                if isinstance(res, JSONResponse):
                    return res
                vinculado, estado_dispositivo, did = res
                telefono = gestor.telefono or ""
                perfil = {
                    "id": str(gestor.id),
                    "usuario": _usuario_de(admin),
                    "nombre": gestor.nombre,
                    "apellido": gestor.apellido or "",
                }
            else:
                perfil = _perfil_admin(admin)
                res = await _enforcement_cuenta(request, session, tenant_id, admin, rol, imei)
                if isinstance(res, JSONResponse):
                    return res
                did = res
            tokens = _par_tokens(secret, tenant_id, admin.id, rol=rol, gestor_id=gestor_id, did=did)
            permisos = await _permisos_de(
                session, tenant_id, {"rol": rol, "gestor_id": str(gestor_id) if gestor_id else None}
            )
    return JSONResponse(
        {
            **tokens,
            "rol": rol,
            "permisos": permisos,
            "perfil": perfil,
            "tenantId": str(tenant_id),
            "tenantSlug": membresia.get("slug"),
            "tenantNombre": membresia.get("nombre"),
            "telefono": telefono,
            "dispositivoVinculado": vinculado,
            "estadoDispositivo": estado_dispositivo,
            "requiereVinculacion": rol == "gestor" and not vinculado,
            "gestor": perfil if rol == "gestor" else None,
            "propietario": perfil if rol != "gestor" else None,
        }
    )


@router.post("/auth/login")
async def login(request: Request) -> JSONResponse:
    """Login unificado (ADR-0081): sin slug ni rol; resuelve membresías del admin.

    Sin `tenant`: 0 negocios → 401; 1 → tokens; varios → `seleccionRequerida` + lista.
    Con `tenant`: emite los tokens del negocio indicado (slug o UUID).
    """
    body = await request.json()
    usuario = str(body.get("usuario") or "").strip()
    password = str(body.get("password") or "")
    ref = str(body.get("tenant") or "").strip()
    imei = str(body.get("imei") or "").strip()
    whatsapp = str(body.get("whatsapp") or "").strip()
    if not (usuario and password):
        return JSONResponse({"error": "faltan datos"}, status_code=400)

    from core.security.login_lockout import (
        client_ip,
        is_blocked,
        register_failure,
        register_success,
    )

    # Mismo bloqueo progresivo que el login por negocio (HU-ARCH-04): este login unificado no lo tenía.
    ip = client_ip(request)
    identificador = usuario.lower()
    if await is_blocked("campo", identificador, ip):
        return JSONResponse({"error": "demasiados intentos, espera unos minutos"}, status_code=429)
    async with get_session() as session:
        admin = await _admin_por_credenciales(session, usuario, password)
    if admin is None:
        bloqueado = await register_failure("campo", identificador, ip)
        from core.access.events import registrar_fallo_de_login

        # Con `tenant` el intento apuntaba a ese negocio: el rastro no avisa a los demás.
        await registrar_fallo_de_login(
            usuario, ip, canal="campo", bloqueado=bloqueado, tenant_id=await _tenant_id_por_ref(ref) if ref else None
        )
        return JSONResponse({"error": "credenciales inválidas"}, status_code=401)
    await register_success("campo", identificador, ip)

    solo = await _tenant_id_por_ref(ref) if ref else None
    if ref and solo is None:
        return JSONResponse({"error": "tenant desconocido"}, status_code=401)
    membresias = await _membresias_de_campo(admin, solo=solo)

    if ref:
        if not membresias:
            return JSONResponse({"error": "sin acceso a ese negocio"}, status_code=403)
        return await _sesion_de_membresia(request, admin, membresias[0], imei, whatsapp)
    if not membresias:
        from core.admin.invitations_service import pendientes_de

        invitaciones = await pendientes_de(admin.id)
        if invitaciones:
            return JSONResponse(
                {
                    "requiereMembresia": True,
                    "membresiaToken": _membresia_token(admin.id),
                    "invitaciones": invitaciones,
                }
            )
        return JSONResponse({"error": "sin acceso a la app de campo"}, status_code=403)
    if len(membresias) == 1:
        return await _sesion_de_membresia(request, admin, membresias[0], imei, whatsapp)
    return JSONResponse(
        {
            "seleccionRequerida": True,
            "negocios": [
                {"slug": m["slug"], "nombre": m["nombre"], "rol": m["rol"]} for m in membresias
            ],
        }
    )


async def _tenant_id_por_ref(ref: str) -> uuid.UUID | None:
    """Resuelve un tenant por slug o UUID (ambos válidos como `tenant`)."""
    cond = Tenant.slug == ref
    if len(ref) == 36:
        try:
            cond = Tenant.id == uuid.UUID(ref)
        except ValueError:
            cond = Tenant.slug == ref
    async with get_session() as session:
        return (
            await session.execute(
                select(Tenant.id).where(
                    cond, Tenant.status == "active", Tenant.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()


@router.post("/auth/gestor/login")
async def gestor_login(request: Request) -> JSONResponse:
    """Compatibilidad (ADR-0081): login del gestor con tenant explícito."""
    return await _login_con_tenant(request, "gestor")


@router.post("/auth/propietario/login")
async def propietario_login(request: Request) -> JSONResponse:
    """Compatibilidad (ADR-0081): login del propietario con tenant explícito."""
    return await _login_con_tenant(request, "propietario")


async def _login_con_tenant(request: Request, rol_esperado: str) -> JSONResponse:
    body = await request.json()
    slug = str(body.get("tenant") or "").strip()
    usuario = str(body.get("usuario") or "").strip()
    password = str(body.get("password") or "")
    imei = str(body.get("imei") or "").strip()
    whatsapp = str(body.get("whatsapp") or "").strip()
    if not (slug and usuario and password):
        return JSONResponse({"error": "faltan datos"}, status_code=400)

    from core.security.login_lockout import (
        client_ip,
        is_blocked,
        register_failure,
        register_success,
    )

    scope = f"gestor:{slug}"
    identificador = usuario.lower()
    ip = client_ip(request)
    if await is_blocked(scope, identificador, ip):
        return JSONResponse({"error": "demasiados intentos, espera unos minutos"}, status_code=429)

    from core.tenants import resolve_tenant_by_slug

    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        await register_failure(scope, None, ip)
        return JSONResponse({"error": "tenant desconocido"}, status_code=401)

    async with get_session() as session:
        admin = await _admin_por_credenciales(session, usuario, password)
    if admin is None:
        bloqueado = await register_failure(scope, identificador, ip)
        from core.access.events import registrar_fallo_de_login

        await registrar_fallo_de_login(usuario, ip, canal="campo", bloqueado=bloqueado, tenant_id=tenant_id)
        if bloqueado:
            from core.shared.audit import record_audit
            from core.shared.tenant import tenant_scope

            async with tenant_scope(tenant_id):
                await record_audit(
                    actor="core:auth",
                    action="login_bloqueado",
                    input_data={"usuario": usuario, "ip": ip},
                )
        return JSONResponse({"error": "credenciales inválidas"}, status_code=401)

    await register_success(scope, identificador, ip)

    membresias = await _membresias_de_campo(admin, solo=tenant_id)
    if not membresias:
        error = "no es gestor de esta cartera" if rol_esperado == "gestor" else "no es propietario de este tenant"
        return JSONResponse({"error": error}, status_code=403)
    membresia = membresias[0]
    if rol_esperado == "gestor" and membresia["rol"] != "gestor":
        return JSONResponse({"error": "no es gestor de esta cartera"}, status_code=403)
    if rol_esperado == "propietario" and membresia["rol"] not in ("propietario", "master"):
        return JSONResponse({"error": "no es propietario de este tenant"}, status_code=403)
    return await _sesion_de_membresia(request, admin, membresia, imei, whatsapp)


@router.get("/auth/invitaciones")
async def invitaciones(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Invitaciones pendientes del usuario (ADR-0081). Acepta token de campo o de membresía."""
    admin_id = await _admin_de_token(authorization)
    if admin_id is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    from core.admin.invitations_service import pendientes_de

    return JSONResponse(await pendientes_de(admin_id))


@router.post("/auth/invitaciones/{invitation_id}/aceptar")
async def aceptar_invitacion(
    invitation_id: uuid.UUID, authorization: str | None = Header(default=None)
) -> JSONResponse:
    admin_id = await _admin_de_token(authorization)
    if admin_id is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    from core.admin.invitations_service import aceptar_invitacion as _aceptar

    error = await _aceptar(invitation_id=invitation_id, admin_id=admin_id)
    if error is not None:
        return JSONResponse({"error": error}, status_code=400)
    return JSONResponse({"ok": True})


@router.post("/auth/invitaciones/{invitation_id}/rechazar")
async def rechazar_invitacion(
    invitation_id: uuid.UUID, authorization: str | None = Header(default=None)
) -> JSONResponse:
    admin_id = await _admin_de_token(authorization)
    if admin_id is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    from core.admin.invitations_service import rechazar_invitacion as _rechazar

    error = await _rechazar(invitation_id=invitation_id, admin_id=admin_id)
    if error is not None:
        return JSONResponse({"error": error}, status_code=400)
    return JSONResponse({"ok": True})


async def _tenant_link(session, tenant_id: uuid.UUID, admin_id: uuid.UUID) -> TenantUser | None:
    return (
        await session.execute(select(TenantUser).where(TenantUser.tenant_id == tenant_id, TenantUser.admin_id == admin_id))
    ).scalar_one_or_none()


@router.post("/auth/refresh")
async def refresh(request: Request) -> JSONResponse:
    body = await request.json()
    token = str(body.get("refreshToken") or "")
    auth = await token_tenant(token)
    if auth is None:
        return JSONResponse({"error": "refresh inválido"}, status_code=401)
    tenant_id, payload = auth
    if payload.get("typ") != "refresh":
        return JSONResponse({"error": "token no es de refresco"}, status_code=401)
    admin_id = payload.get("admin_id")
    if not admin_id:
        return JSONResponse({"error": "token sin admin"}, status_code=401)
    async with tenant_scope(tenant_id):
        secret = await pack_secret()
        if not secret:
            return JSONResponse({"error": "pack sin secreto de firma"}, status_code=503)
        async with get_session() as session:
            admin = (await session.execute(select(Admin).where(Admin.id == uuid.UUID(str(admin_id)), Admin.active.is_(True)))).scalar_one_or_none()
            if admin is None or not cuenta_habilitada(admin):
                return JSONResponse({"error": "admin inactivo"}, status_code=401)
    # El rol y el gestor se RECALCULAN (ADR-0136), no se copian del token viejo: un negocio
    # suspendido, una cuenta desvinculada o un owner bajado a colaborador no renuevan con
    # el acceso que tenían.
    membresias = await _membresias_de_campo(admin, solo=tenant_id)
    if not membresias:
        return JSONResponse({"error": "sin acceso a ese negocio"}, status_code=401)
    membresia = membresias[0]
    gestor_id = uuid.UUID(str(membresia["gestorId"])) if membresia.get("gestorId") else None
    did: uuid.UUID | None = None
    rol_politica = {"gestor": "gestor", "propietario": "owner"}.get(str(membresia["rol"]))
    if rol_politica:
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                if rol_politica == "gestor":
                    gestor = await get_owned(session, Gestor, gestor_id) if gestor_id else None
                    if gestor is None or gestor.estatus != "activo":
                        return JSONResponse({"error": "no es gestor de esta cartera"}, status_code=403)
                res = await acceso.equipo_del_refresh(
                    request, session, tenant_id, admin.id, rol_politica, payload.get("did")
                )
                if isinstance(res, JSONResponse):
                    return res
                did = res
    tokens = _par_tokens(secret, tenant_id, admin.id, rol=str(membresia["rol"]), gestor_id=gestor_id, did=did)
    return JSONResponse(tokens)


@router.patch("/perfil")
async def actualizar_perfil(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    body = await request.json()
    nombre = str(body.get("nombre") or "").strip()
    apellido = str(body.get("apellido") or "").strip()
    if not nombre:
        return JSONResponse({"error": "nombre requerido"}, status_code=400)
    gestor_id = payload.get("gestor_id")
    admin_id = payload.get("admin_id")
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if gestor_id:
                gestor = await get_owned(session, Gestor, uuid.UUID(str(gestor_id)))
                if gestor is None:
                    return JSONResponse({"error": "gestor no encontrado"}, status_code=404)
                gestor.nombre = nombre
                gestor.apellido = apellido or None
                perfil = {"id": str(gestor.id), "usuario": "", "nombre": gestor.nombre, "apellido": gestor.apellido or ""}
            elif admin_id:
                admin = (await session.execute(select(Admin).where(Admin.id == uuid.UUID(str(admin_id))))).scalar_one_or_none()
                if admin is None:
                    return JSONResponse({"error": "usuario no encontrado"}, status_code=404)
                perfil = {"id": str(admin.id), "usuario": _usuario_de(admin), "nombre": nombre, "apellido": apellido}
            else:
                return JSONResponse({"error": "token sin identidad"}, status_code=401)
    return JSONResponse(perfil)


# --------------------------------------------------------------- dispositivo (HU-39/43)
@router.post("/gestor/dispositivo")
async def vincular_dispositivo(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Pide vincular este equipo a la cuenta del gestor; según la política del negocio queda
    **pendiente** de aprobación (o activo si es abierta). Idempotente por (cuenta, equipo)."""
    auth = await _auth(authorization, sin_equipo_ok=True)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    gestor_id = payload.get("gestor_id")
    if not gestor_id:
        return JSONResponse({"error": "token sin gestor"}, status_code=403)
    body = await request.json()
    device_id, via_header = acceso.equipo_de(request, str(body.get("imei") or ""))
    whatsapp = str(body.get("whatsapp") or "").strip()
    # La app vieja exige el contacto; la nueva identifica el equipo por header y el contacto es opcional.
    if not device_id or (not via_header and not whatsapp):
        return JSONResponse({"error": "imei y whatsapp son obligatorios"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            gestor = await get_owned(session, Gestor, uuid.UUID(str(gestor_id)))
            if gestor is None or gestor.admin_id is None:
                return JSONResponse({"error": "gestor no encontrado"}, status_code=404)
            await acceso.adoptar_legado(session, tenant_id, gestor)
            existia = (
                await session.execute(
                    select(AccountDevice.id).where(
                        AccountDevice.tenant_id == tenant_id,
                        AccountDevice.admin_id == gestor.admin_id,
                        AccountDevice.device_id == device_id,
                        AccountDevice.deleted_at.is_(None),
                    )
                )
            ).first() is not None
            try:
                decision = await evaluar(
                    session,
                    tenant_id=tenant_id,
                    admin_id=gestor.admin_id,
                    rol="gestor",
                    device_id=device_id,
                    canal="campo" if via_header else "campo_v1",
                    ip=request.client.host if request.client else None,
                    detalle_alta={"contacto": whatsapp[: acceso.MAX_CONTACTO]} if whatsapp else None,
                )
            except AccesoError as exc:
                return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status)
    return JSONResponse(
        {"estado": decision.estado, "imei": device_id, "whatsapp": whatsapp or None},
        status_code=200 if existia else 201,
    )


@router.get("/gestor/dispositivo")
async def estado_dispositivo(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Estado de vinculación del equipo del gestor (la app espera la aprobación). Con `X-Device-Id`, el de
    ESE equipo; sin él, el mejor estado entre los de la cuenta (contrato anterior)."""
    auth = await _auth(authorization, sin_equipo_ok=True)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    gestor_id = payload.get("gestor_id")
    if not gestor_id:
        return JSONResponse({"error": "token sin gestor"}, status_code=403)
    device_id, _ = acceso.equipo_de(request)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            gestor = await get_owned(session, Gestor, uuid.UUID(str(gestor_id)))
            if gestor is None or gestor.admin_id is None:
                return JSONResponse({"error": "gestor no encontrado"}, status_code=404)
            await acceso.adoptar_legado(session, tenant_id, gestor)
            consulta = select(AccountDevice).where(
                AccountDevice.tenant_id == tenant_id,
                AccountDevice.admin_id == gestor.admin_id,
                AccountDevice.deleted_at.is_(None),
            )
            if device_id:
                consulta = consulta.where(AccountDevice.device_id == device_id)
            devices = (await session.execute(consulta.order_by(AccountDevice.created_at.desc()))).scalars().all()
    activo = next((d for d in devices if d.estado == "activo"), None)
    pendiente = next((d for d in devices if d.estado == "pendiente"), None)
    bloqueado = next((d for d in devices if d.estado == "bloqueado"), None) if device_id else None
    dev = activo or pendiente or bloqueado
    return JSONResponse(
        {
            "vinculado": activo is not None,
            "estado": dev.estado if dev else "none",
            "imei": dev.device_id if dev else None,
            "whatsapp": None,
        }
    )


# --------------------------------------------------------------- lecturas (F2)
def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def _d(v: date | datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


def _hoy() -> date:
    return date.today()


async def _carteras_visibles(session, tenant_id: uuid.UUID, payload: dict[str, Any]) -> list[Cartera]:
    """Carteras que el token puede ver: todas con acceso total; las suyas para el gestor.

    Acceso total (todas las carteras del tenant): `master`, `propietario` y cualquier
    rol distinto de `gestor` (ADR-0081). El gestor solo ve las carteras asignadas.
    """
    q = select(Cartera).where(Cartera.tenant_id == tenant_id, Cartera.deleted_at.is_(None)).order_by(Cartera.nombre)
    if payload.get("rol") == "gestor":
        gid = payload.get("gestor_id")
        if not gid:
            return []
        q = q.where(Cartera.gestor_id == uuid.UUID(str(gid)))
    return list((await session.execute(q)).scalars().all())


async def _cartera_visible(session, tenant_id: uuid.UUID, payload: dict[str, Any], cartera_id: uuid.UUID) -> Cartera | None:
    return next((c for c in await _carteras_visibles(session, tenant_id, payload) if c.id == cartera_id), None)


async def _permisos_de(session, tenant_id: uuid.UUID, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("rol") != "gestor":
        return [{"permiso": p, "habilitado": True} for p in GESTOR_PERMISOS]
    gid = payload.get("gestor_id")
    if not gid:
        return []
    rows = (await session.execute(select(GestorPermiso).where(GestorPermiso.tenant_id == tenant_id, GestorPermiso.gestor_id == uuid.UUID(str(gid))))).scalars().all()
    habilitados = {r.permiso: r.habilitado for r in rows}
    return [{"permiso": p, "habilitado": bool(habilitados.get(p, False))} for p in GESTOR_PERMISOS]


def _config_public(cartera_id: uuid.UUID, cfg: CarteraConfig | None) -> dict[str, Any]:
    def g(attr: str, default: Any) -> Any:
        return getattr(cfg, attr, default) if cfg is not None else default

    return {
        "carteraId": str(cartera_id),
        "cuotasMinimasPrestamo": int(g("cuotas_minimas_prestamo", 0)),
        "cuotasAtrasoUmbral": int(g("cuotas_atraso_umbral", 0)),
        "manejoCupoActivo": bool(g("manejo_cupo_activo", False)),
        "cupoDefault": _f(g("cupo_default", 0)),
        "recargoActivo": bool(g("recargo_activo", False)),
        "bloquearCambioInteres": bool(g("bloquear_cambio_interes", False)),
        "comisionActiva": bool(g("comision_activa", False)),
        "comisionPorcentaje": _f(g("comision_porcentaje", 0)),
        "mostrarFechaUltimaLiquidada": bool(g("mostrar_fecha_ultima_liquidada", False)),
        "mostrarCaja": bool(g("mostrar_caja", False)),
        "mostrarCobradoLiquidada": bool(g("mostrar_cobrado_liquidada", False)),
        "mostrarPrestamos": bool(g("mostrar_prestamos", True)),
        "eliminarPrestamosApk": bool(g("eliminar_prestamos_apk", False)),
        "reconocimientoFacialActivo": bool(g("reconocimiento_facial_activo", False)),
        "registroDocumentoCliente": bool(g("registro_documento_cliente", False)),
        "eliminarPagosApk": bool(g("eliminar_pagos_apk", False)),
        "eliminarGastosApk": bool(g("eliminar_gastos_apk", False)),
        "eliminarInyeccionApk": bool(g("eliminar_inyeccion_apk", False)),
        "eliminarAbonosApk": bool(g("eliminar_abonos_apk", False)),
        "registrarInyeccionApk": bool(g("registrar_inyeccion_apk", False)),
        "generarReportesApk": bool(g("generar_reportes_apk", False)),
        "ocultarCartera": bool(g("ocultar_cartera", False)),
        "mostrarCobroEstimado": bool(g("mostrar_cobro_estimado", True)),
        "bloqueoAutomaticoClientes": bool(g("bloqueo_automatico_clientes", False)),
        "permitirCambioFechaPrestamo": bool(g("permitir_cambio_fecha_prestamo", False)),
        "borrarClientesSinDeuda": bool(g("borrar_clientes_sin_deuda", False)),
        "periodoLiquidacion": str(g("periodo_liquidacion", "diario")),
        "diasNoLaborables": str(g("dias_no_laborables", "solo_domingos")),
        "metodosPago": ["efectivo", "transferencia", "qr"],
        "diasAnticipacionNotificacion": int(g("dias_anticipacion_notificacion", 0)),
        "avisoDiaCobro": bool(g("aviso_dia_cobro", False)),
        "umbralMoraNotificacion": int(g("umbral_mora_notificacion", 0)),
    }


def _cuota_public(c: Cuota) -> dict[str, Any]:
    return {"id": str(c.id), "numeroCuota": c.numero_cuota, "valorEsperado": _f(c.valor_esperado), "fechaVencimiento": _d(c.fecha_vencimiento), "estatus": c.estatus}


def _dias_mora(cuotas: list[Cuota]) -> int:
    hoy = _hoy()
    return max(((hoy - c.fecha_vencimiento).days for c in cuotas if c.estatus != "pagada" and c.fecha_vencimiento < hoy), default=0)


def _evidencia_url(cliente_id: uuid.UUID, ev: ClienteEvidencia | None) -> str | None:
    return f"/clientes/{cliente_id}/evidencia/{ev.id}" if ev is not None else None


async def _foto(session, tenant_id: uuid.UUID, cliente_id: uuid.UUID, tipo: str = "foto_facial") -> str | None:
    ev = (
        await session.execute(select(ClienteEvidencia).where(ClienteEvidencia.tenant_id == tenant_id, ClienteEvidencia.cliente_id == cliente_id, ClienteEvidencia.tipo == tipo).order_by(ClienteEvidencia.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    return _evidencia_url(cliente_id, ev)


@router.get("/gestor/mis-carteras")
async def mis_carteras(authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    from pack_cobranza.pagos import config_del_tenant

    async with tenant_scope(tenant_id):
        cfg_pack = await config_del_tenant()
        qr_activo = bool(cfg_pack.pago_qr_activo) if cfg_pack is not None else False
        async with get_session() as session:
            permisos = await _permisos_de(session, tenant_id, payload)
            out = []
            for c in await _carteras_visibles(session, tenant_id, payload):
                cfg = (await session.execute(select(CarteraConfig).where(CarteraConfig.tenant_id == tenant_id, CarteraConfig.cartera_id == c.id))).scalar_one_or_none()
                out.append({"id": str(c.id), "nombre": c.nombre, "estatus": c.estatus, "tipoInteres": _f(c.tipo_interes), "numCuotas": c.num_cuotas, "pagoQrActivo": qr_activo, "config": _config_public(c.id, cfg), "permisos": permisos})
    return JSONResponse(out)


@router.get("/gestor/notificaciones")
async def notificaciones(authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            visibles = {c.id: c.nombre for c in await _carteras_visibles(session, tenant_id, payload)}
            cliente_nombre = {c.id: f"{c.nombre} {c.apellido or ''}".strip() for c in (await session.execute(select(Cliente).where(Cliente.tenant_id == tenant_id))).scalars().all()}
            items: list[dict[str, Any]] = []

            for p in (await session.execute(select(Pago).where(Pago.tenant_id == tenant_id, Pago.deleted_at.is_(None)).order_by(Pago.fecha_hora.desc()).limit(30))).scalars().all():
                items.append({"id": str(p.id), "tipo": "pago", "mensaje": f"Pago registrado: {_f(p.valor):,.0f}", "clienteId": str(p.cliente_id), "monto": _f(p.valor), "fecha": _d(p.fecha_hora)})
            for g in (await session.execute(select(Gasto).where(Gasto.tenant_id == tenant_id, Gasto.deleted_at.is_(None)).order_by(Gasto.fecha_hora.desc()).limit(20))).scalars().all():
                items.append({"id": str(g.id), "carteraId": str(g.cartera_id), "tipo": "gasto", "mensaje": f"Gasto: {g.descripcion}", "clienteId": None, "monto": _f(g.valor), "fecha": _d(g.fecha_hora)})
            for v in (await session.execute(select(Visita).where(Visita.tenant_id == tenant_id, Visita.resultado == "no_pago", Visita.deleted_at.is_(None)).order_by(Visita.created_at.desc()).limit(20))).scalars().all():
                if v.cartera_id not in visibles:
                    continue
                items.append({"id": str(v.id), "carteraId": str(v.cartera_id), "tipo": "no_pago", "mensaje": f"Sin pago: {cliente_nombre.get(v.cliente_id, 'cliente')}", "clienteId": str(v.cliente_id), "monto": None, "fecha": _d(v.created_at)})
            for liq in (await session.execute(select(Liquidacion).where(Liquidacion.tenant_id == tenant_id, Liquidacion.deleted_at.is_(None)).order_by(Liquidacion.created_at.desc()).limit(10))).scalars().all():
                if liq.cartera_id not in visibles:
                    continue
                items.append({"id": str(liq.id), "carteraId": str(liq.cartera_id), "tipo": "liquidacion", "mensaje": f"Liquidación {liq.periodo}", "clienteId": None, "monto": _f(liq.total_cobrado), "fecha": _d(liq.fecha)})

            def clave(it: dict[str, Any]) -> str:
                return it.get("fecha") or ""

            items.sort(key=clave, reverse=True)
            cliente_a_cartera = {c.id: c.cartera_id for c in (await session.execute(select(Cliente).where(Cliente.tenant_id == tenant_id))).scalars().all()}
            for it in items:
                cid = it.get("carteraId")
                if cid is None and it.get("clienteId"):
                    car = cliente_a_cartera.get(uuid.UUID(str(it["clienteId"])))
                    it["carteraId"] = str(car) if car else ""
                it["carteraNombre"] = visibles.get(uuid.UUID(str(it.get("carteraId"))), "") if it.get("carteraId") else ""
            items = [it for it in items if it.get("carteraId") and uuid.UUID(str(it["carteraId"])) in visibles]
    return JSONResponse(items[:60])


async def _trayecto_data(session, tenant_id: uuid.UUID, cid: uuid.UUID) -> dict[str, Any]:
    """Clientes del día + último trayecto (compartido por el endpoint y el offline)."""
    hoy = _hoy()
    clientes = (await session.execute(select(Cliente).where(Cliente.tenant_id == tenant_id, Cliente.cartera_id == cid, Cliente.deleted_at.is_(None)).order_by(Cliente.nombre))).scalars().all()
    out = []
    for c in clientes:
        prestamos = (await session.execute(select(Prestamo).where(Prestamo.tenant_id == tenant_id, Prestamo.cliente_id == c.id, Prestamo.estatus == "vigente", Prestamo.deleted_at.is_(None)))).scalars().all()
        if not prestamos:
            continue
        cuotas = (await session.execute(select(Cuota).where(Cuota.tenant_id == tenant_id, Cuota.prestamo_id.in_([p.id for p in prestamos]), Cuota.estatus.in_(("pendiente", "atrasada")), Cuota.deleted_at.is_(None)))).scalars().all()
        mora = _dias_mora(cuotas)
        visita_hoy = (await session.execute(select(Visita).where(Visita.tenant_id == tenant_id, Visita.cliente_id == c.id, Visita.fecha == hoy, Visita.deleted_at.is_(None)).limit(1))).scalar_one_or_none()
        promesa = (await session.execute(select(Promesa).where(Promesa.tenant_id == tenant_id, Promesa.cliente_id == c.id, Promesa.estado == "pendiente", Promesa.deleted_at.is_(None)).order_by(Promesa.fecha_prometida.desc()).limit(1))).scalar_one_or_none()
        color = "rojo" if mora > 0 else ("verde" if (visita_hoy and visita_hoy.resultado == "pago") else "blanco")
        out.append({"clienteId": str(c.id), "nombre": f"{c.nombre} {c.apellido or ''}".strip(), "latitud": c.lat, "longitud": c.lng, "enTrayecto": True, "color": color, "visitaRegistrada": visita_hoy is not None, "diasMora": mora, "compromisoValor": _f(promesa.valor_prometido) if promesa else None})
    log = (await session.execute(select(CarteraOptimizadaLog).where(CarteraOptimizadaLog.tenant_id == tenant_id, CarteraOptimizadaLog.cartera_id == cid).order_by(CarteraOptimizadaLog.created_at.desc()).limit(1))).scalar_one_or_none()
    trayecto = None
    if log is not None:
        por_id = {str(c.id): c for c in clientes}
        ids = [i for i in (log.orden_clientes or []) if isinstance(i, str) and i in por_id]
        orden = [{"clienteId": i, "latitud": por_id[i].lat, "longitud": por_id[i].lng} for i in ids]
        # La APK tipa `ordenClientes` como lista de trayectos (ParadaGeo[][]).
        trayecto = {"id": str(log.id), "carteraId": str(cid), "fecha": _d(log.fecha), "ordenClientes": [orden] if orden else [], "waypoints": [{"latitud": p["latitud"], "longitud": p["longitud"]} for p in orden if p["latitud"] is not None and p["longitud"] is not None], "distanciaEstimadaKm": _f(log.distancia_estimada_km), "tiempoEstimadoMin": int(log.tiempo_estimado_min or 0), "tipo": log.tipo}
    return {"clientes": out, "trayectos": trayecto}


@router.get("/gestor/carteras/{cartera_id}/trayecto-diario")
async def trayecto_diario(cartera_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            data = await _trayecto_data(session, tenant_id, cid)
    return JSONResponse(data)


@router.get("/gestor/carteras/{cartera_id}/estadisticas")
async def estadisticas_cartera(cartera_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    hoy = _hoy()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            conteos = await conteos_cartera(session, tenant_id, cid)
            snap = (await session.execute(select(CarteraEstadisticasSnapshot).where(CarteraEstadisticasSnapshot.tenant_id == tenant_id, CarteraEstadisticasSnapshot.cartera_id == cid).order_by(CarteraEstadisticasSnapshot.fecha.desc()).limit(1))).scalar_one_or_none()
            anterior = None
            deltas: dict[str, Any] = {}
            if snap is not None and isinstance(snap.datos, dict) and "conteos" in snap.datos:
                anterior = snap.datos.get("conteos")
                deltas = {k: (conteos[k] - int((anterior or {}).get(k) or 0)) for k in conteos}
    return JSONResponse({"carteraId": str(cid), "fecha": hoy.isoformat(), "actual": conteos, "anterior": anterior, "deltas": deltas, "actualizadoEn": datetime.utcnow().isoformat()})


def _cliente_gestor(c: Cliente, num_prestamos: int, dias_mora: int, foto: str | None) -> dict[str, Any]:
    return {
        "id": str(c.id), "carteraId": str(c.cartera_id), "nombre": c.nombre, "apellido": c.apellido or "",
        "negocio": c.negocio, "telefonoWhatsapp": c.telefono_whatsapp or "",
        "latitud": c.lat, "longitud": c.lng, "latitudDomicilio": c.lat_domicilio, "longitudDomicilio": c.lng_domicilio,
        "topeMaximoDeuda": _f(c.tope_maximo_deuda) if c.tope_maximo_deuda is not None else None,
        "estatus": c.estatus, "colorRiesgo": c.color_riesgo, "fotoUrl": foto,
        "tipoDocumento": c.tipo_documento, "numeroDocumento": c.numero_documento,
        "numPrestamos": num_prestamos, "diasMora": dias_mora, "createdAt": _d(c.created_at),
    }


async def _prestamos_de(session, tenant_id: uuid.UUID, cliente_id: uuid.UUID, *, solo_vigentes: bool = False) -> list[Prestamo]:
    q = select(Prestamo).where(Prestamo.tenant_id == tenant_id, Prestamo.cliente_id == cliente_id, Prestamo.deleted_at.is_(None)).order_by(Prestamo.created_at.desc())
    if solo_vigentes:
        q = q.where(Prestamo.estatus == "vigente")
    return list((await session.execute(q)).scalars().all())


async def _saldo_y_mora(session, tenant_id: uuid.UUID, prestamos: list[Prestamo]) -> tuple[float, int]:
    if not prestamos:
        return 0.0, 0
    cuotas = list((await session.execute(select(Cuota).where(Cuota.tenant_id == tenant_id, Cuota.prestamo_id.in_([p.id for p in prestamos]), Cuota.deleted_at.is_(None)))).scalars().all())
    saldo = sum((_f(c.valor_esperado) for c in cuotas if c.estatus != "pagada"), 0.0)
    return saldo, _dias_mora(cuotas)


async def _cliente_o_404(session, tenant_id: uuid.UUID, cartera_id: uuid.UUID, cliente_id: uuid.UUID) -> Cliente | None:
    cli = await get_owned(session, Cliente, cliente_id)
    return cli if cli is not None and cli.cartera_id == cartera_id else None


@router.get("/gestor/carteras/{cartera_id}/clientes")
async def clientes_de_cartera(cartera_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            clientes = (await session.execute(select(Cliente).where(Cliente.tenant_id == tenant_id, Cliente.cartera_id == cid, Cliente.deleted_at.is_(None)).order_by(Cliente.nombre))).scalars().all()
            out = []
            for c in clientes:
                prestamos = await _prestamos_de(session, tenant_id, c.id, solo_vigentes=True)
                _, mora = await _saldo_y_mora(session, tenant_id, prestamos)
                out.append(_cliente_gestor(c, len(prestamos), mora, await _foto(session, tenant_id, c.id)))
    return JSONResponse(out)


@router.get("/gestor/carteras/{cartera_id}/clientes/{cliente_id}/tarjeta")
async def tarjeta(cartera_id: str, cliente_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid, clid = uuid.UUID(cartera_id), uuid.UUID(cliente_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            c = await _cliente_o_404(session, tenant_id, cid, clid)
            if c is None:
                return JSONResponse({"error": "cliente no encontrado"}, status_code=404)
            prestamos = await _prestamos_de(session, tenant_id, c.id, solo_vigentes=True)
            saldo, mora = await _saldo_y_mora(session, tenant_id, prestamos)
    return JSONResponse({
        "clienteId": str(c.id), "carteraId": str(c.cartera_id), "nombre": f"{c.nombre} {c.apellido or ''}".strip(),
        "negocio": c.negocio, "telefonoWhatsapp": c.telefono_whatsapp or "", "color": c.color_riesgo,
        "fotoUrl": await _foto(session, tenant_id, c.id), "documentoFrenteUrl": await _foto(session, tenant_id, c.id, "documento_frente"),
        "documentoReversoUrl": await _foto(session, tenant_id, c.id, "documento_reverso"),
        "tipoDocumento": c.tipo_documento, "numeroDocumento": c.numero_documento, "latitud": c.lat, "longitud": c.lng,
        "latitudDomicilio": c.lat_domicilio, "longitudDomicilio": c.lng_domicilio, "tipoPago": None,
        "saldoPendiente": saldo, "diasMora": mora,
    })


@router.get("/gestor/carteras/{cartera_id}/clientes/{cliente_id}/prestamos")
async def prestamos_de_cliente(cartera_id: str, cliente_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid, clid = uuid.UUID(cartera_id), uuid.UUID(cliente_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            prestamos = await _prestamos_de(session, tenant_id, clid)
            out = []
            for p in prestamos:
                cuotas = list((await session.execute(select(Cuota).where(Cuota.tenant_id == tenant_id, Cuota.prestamo_id == p.id, Cuota.deleted_at.is_(None)).order_by(Cuota.numero_cuota))).scalars().all())
                out.append({"id": str(p.id), "carteraId": str(p.cartera_id), "clienteId": str(p.cliente_id), "valor": _f(p.valor), "numCuotas": p.num_cuotas, "tipoInteres": _f(p.tipo_interes), "diasEntreCuotas": p.dias_entre_cuotas, "fechaOtorgado": _d(p.fecha_otorgado), "estatus": p.estatus, "cuotas": [_cuota_public(c) for c in cuotas]})
    return JSONResponse(out)


@router.get("/gestor/carteras/{cartera_id}/prestamos/{prestamo_id}/estado-cuenta")
async def estado_cuenta(cartera_id: str, prestamo_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid, pid = uuid.UUID(cartera_id), uuid.UUID(prestamo_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            p = await get_owned(session, Prestamo, pid)
            if p is None or p.cartera_id != cid:
                return JSONResponse({"error": "préstamo no encontrado"}, status_code=404)
            cliente = await get_owned(session, Cliente, p.cliente_id)
            cartera = await get_owned(session, Cartera, cid)
            cuotas = (await session.execute(select(Cuota).where(Cuota.tenant_id == tenant_id, Cuota.prestamo_id == pid, Cuota.deleted_at.is_(None)).order_by(Cuota.numero_cuota))).scalars().all()
            pagos = (await session.execute(select(Pago).where(Pago.tenant_id == tenant_id, Pago.prestamo_id == pid, Pago.deleted_at.is_(None)))).scalars().all()
            abonos = (await session.execute(select(Abono).where(Abono.tenant_id == tenant_id, Abono.prestamo_id == pid, Abono.deleted_at.is_(None)))).scalars().all()
            pagos_por_cuota: dict[uuid.UUID, float] = {}
            for pg in pagos:
                if pg.cuota_id:
                    pagos_por_cuota[pg.cuota_id] = pagos_por_cuota.get(pg.cuota_id, 0.0) + _f(pg.valor)
            cuotas_out = []
            saldo_total = 0.0
            for c in cuotas:
                pagado = pagos_por_cuota.get(c.id, 0.0)
                saldo_c = max(_f(c.valor_esperado) - pagado, 0.0)
                saldo_total += saldo_c if c.estatus != "pagada" else 0.0
                cuotas_out.append({"cuotaId": str(c.id), "numeroCuota": c.numero_cuota, "valorEsperado": _f(c.valor_esperado), "fechaVencimiento": _d(c.fecha_vencimiento), "estatus": c.estatus, "abonosAcumulados": pagado, "saldoPendiente": saldo_c})
            proximo = next((c.fecha_vencimiento for c in cuotas if c.estatus != "pagada"), None)
    return JSONResponse({
        "prestamoId": str(p.id), "carteraId": str(p.cartera_id), "clienteId": str(p.cliente_id),
        "nombreCliente": f"{cliente.nombre} {cliente.apellido or ''}".strip() if cliente else "",
        "valor": _f(p.valor), "numCuotas": p.num_cuotas, "tipoInteres": _f(p.tipo_interes), "estatus": p.estatus,
        "moneda": cartera.moneda if cartera else "COP", "cuotas": cuotas_out,
        "totalAbonos": sum((_f(a.valor) for a in abonos), 0.0), "saldoPendiente": saldo_total, "proximoVencimiento": _d(proximo),
    })


@router.get("/gestor/carteras/{cartera_id}/prestamos/{prestamo_id}/cuotas/{cuota_id}/detalle")
async def detalle_cuota(cartera_id: str, prestamo_id: str, cuota_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid, pid, quid = uuid.UUID(cartera_id), uuid.UUID(prestamo_id), uuid.UUID(cuota_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            cuota = await get_owned(session, Cuota, quid)
            if cuota is None or cuota.prestamo_id != pid:
                return JSONResponse({"error": "cuota no encontrada"}, status_code=404)
            pagos = (await session.execute(select(Pago).where(Pago.tenant_id == tenant_id, Pago.cuota_id == quid, Pago.deleted_at.is_(None)).order_by(Pago.fecha_hora))).scalars().all()
            abonos = (await session.execute(select(Abono).where(Abono.tenant_id == tenant_id, Abono.prestamo_id == pid, Abono.deleted_at.is_(None)).order_by(Abono.fecha_hora))).scalars().all()
            ultima = (await session.execute(select(Visita).where(Visita.tenant_id == tenant_id, Visita.cliente_id == cuota.id, Visita.deleted_at.is_(None)).order_by(Visita.created_at.desc()).limit(1))).scalar_one_or_none()
            pagado = sum((_f(p.valor) for p in pagos), 0.0)
    return JSONResponse({
        "cuotaId": str(cuota.id), "numeroCuota": cuota.numero_cuota, "valorEsperado": _f(cuota.valor_esperado),
        "fechaVencimiento": _d(cuota.fecha_vencimiento), "estatus": cuota.estatus, "abonosAcumulados": pagado,
        "saldoPendiente": max(_f(cuota.valor_esperado) - pagado, 0.0),
        "pagos": [{"id": str(p.id), "valor": _f(p.valor), "metodoPago": p.metodo_pago, "fechaHora": _d(p.fecha_hora), "registradoPor": p.registrado_por, "liquidado": False} for p in pagos],
        "abonos": [{"id": str(a.id), "valor": _f(a.valor), "fechaHora": _d(a.fecha_hora), "liquidado": False} for a in abonos],
        "ultimaVisita": {"resultado": ultima.resultado, "motivoNoPago": ultima.motivo_no_pago, "fecha": _d(ultima.fecha)} if ultima else None,
    })


@router.post("/gestor/carteras/{cartera_id}/cuotas/{cuota_id}/link-pago")
async def crear_link_pago_cuota(cartera_id: str, cuota_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Crea (o reutiliza) el link/QR de cobro de una cuota (REQ-APK-007, ADR-0067)."""
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid, quid = uuid.UUID(cartera_id), uuid.UUID(cuota_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    from pack_cobranza.pagos import PaymentCapabilityError, config_del_tenant, crear_link_para_cuota

    async with tenant_scope(tenant_id):
        cfg = await config_del_tenant()
        if cfg is None or not cfg.pago_qr_activo:
            return JSONResponse({"error": "El cobro con QR no está habilitado."}, status_code=403)
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            cuota = await get_owned(session, Cuota, quid)
            prestamo = await get_owned(session, Prestamo, cuota.prestamo_id) if cuota else None
            if cuota is None or prestamo is None or prestamo.cartera_id != cid:
                return JSONResponse({"error": "cuota no encontrada"}, status_code=404)
        try:
            data = await crear_link_para_cuota(cuota, cfg)
        except PaymentCapabilityError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(data, status_code=201)


@router.get("/gestor/carteras/{cartera_id}/notas")
async def notas(cartera_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            rows = (await session.execute(select(CarteraNota).where(CarteraNota.tenant_id == tenant_id, CarteraNota.cartera_id == cid, CarteraNota.deleted_at.is_(None)).order_by(CarteraNota.created_at.desc()))).scalars().all()
    return JSONResponse([{"id": str(n.id), "carteraId": str(n.cartera_id), "nota": n.nota, "creadoPorRol": n.creado_por_rol or "", "creadoPorId": str(n.creado_por_id) if n.creado_por_id else None, "createdAt": _d(n.created_at), "updatedAt": _d(n.updated_at)} for n in rows])


@router.get("/gestor/carteras/{cartera_id}/liquidaciones")
async def liquidaciones(cartera_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            rows = (await session.execute(select(Liquidacion).where(Liquidacion.tenant_id == tenant_id, Liquidacion.cartera_id == cid, Liquidacion.deleted_at.is_(None)).order_by(Liquidacion.fecha.desc()))).scalars().all()
    return JSONResponse([_liquidacion_public(liq) for liq in rows])


@router.get("/clientes/{cliente_id}/evidencia/{ev_id}")
async def servir_evidencia(cliente_id: str, ev_id: str, authorization: str | None = Header(default=None)) -> Any:
    """Sirve la evidencia del cliente a la app de campo (Bearer), tenant-scoped."""
    from fastapi.responses import FileResponse

    from pack_cobranza.evidencias import ruta_evidencia

    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, _payload = auth
    try:
        clid, evid = uuid.UUID(cliente_id), uuid.UUID(ev_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            ev = await get_owned(session, ClienteEvidencia, evid)
            if ev is None or ev.cliente_id != clid:
                return JSONResponse({"error": "evidencia no encontrada"}, status_code=404)
            ruta = await ruta_evidencia(tenant_id, ev.ruta_archivo)
            if ruta is None:
                return JSONResponse({"error": "archivo no disponible"}, status_code=404)
            return FileResponse(ruta, media_type=ev.mimetype or "application/octet-stream")


# --------------------------------------------------------------- escrituras (F3)
def _auditar(session, tenant_id: uuid.UUID, entidad: str, entidad_id: uuid.UUID, operacion: str, *, antes: Any = None, despues: Any = None, motivo: str | None = None) -> None:
    session.add(AuditoriaCartera(tenant_id=tenant_id, entidad=entidad, entidad_id=entidad_id, operacion=operacion, valores_antes=antes, valores_despues=despues, actor_rol="gestor", motivo=motivo))


async def _password_ok(session, payload: dict[str, Any], password: str) -> bool:
    admin_id = payload.get("admin_id")
    if not admin_id or not password:
        return False
    admin = (await session.execute(select(Admin).where(Admin.id == uuid.UUID(str(admin_id)), Admin.active.is_(True)))).scalar_one_or_none()
    return admin is not None and cuenta_habilitada(admin) and verify_password(password, admin.password_hash)


async def _caja(session, tenant_id: uuid.UUID, cartera_id: uuid.UUID) -> Caja:
    caja = (await session.execute(select(Caja).where(Caja.tenant_id == tenant_id, Caja.cartera_id == cartera_id))).scalar_one_or_none()
    if caja is None:
        caja = Caja(tenant_id=tenant_id, cartera_id=cartera_id, saldo_inicial=Decimal(0), saldo_actual=Decimal(0))
        session.add(caja)
        await session.flush()
    return caja


def _prestamo_public(p: Prestamo, cuotas: list[Cuota]) -> dict[str, Any]:
    return {"id": str(p.id), "carteraId": str(p.cartera_id), "clienteId": str(p.cliente_id), "valor": _f(p.valor), "numCuotas": p.num_cuotas, "tipoInteres": _f(p.tipo_interes), "diasEntreCuotas": p.dias_entre_cuotas, "fechaOtorgado": _d(p.fecha_otorgado), "estatus": p.estatus, "cuotas": [_cuota_public(c) for c in cuotas]}


def _visita_public(v: Visita, prestamo_id: uuid.UUID | None) -> dict[str, Any]:
    return {"id": str(v.id), "carteraId": str(v.cartera_id), "clienteId": str(v.cliente_id), "prestamoPrincipalId": str(prestamo_id) if prestamo_id else "", "fecha": _d(v.fecha), "resultado": v.resultado, "motivoNoPago": v.motivo_no_pago, "valorPagado": _f(v.valor_pagado) if v.valor_pagado is not None else None, "metodoPago": v.metodo_pago}


def _gasto_public(g: Gasto, evidencias: list[GastoEvidencia]) -> dict[str, Any]:
    return {"id": str(g.id), "carteraId": str(g.cartera_id), "descripcion": g.descripcion, "valor": _f(g.valor), "aprobado": g.aprobado, "aprobadoPor": str(g.aprobado_por) if g.aprobado_por else None, "estado": g.estado, "fechaHora": _d(g.fecha_hora), "evidencias": [{"id": str(e.id), "gastoId": str(e.gasto_id), "nombreOriginal": e.nombre_original or "", "mimetype": e.mimetype or "", "tamaño": e.tamano or 0, "carteraArchivo": e.ruta_archivo} for e in evidencias]}


def _nota_public(n: CarteraNota) -> dict[str, Any]:
    return {"id": str(n.id), "carteraId": str(n.cartera_id), "nota": n.nota, "creadoPorRol": n.creado_por_rol or "", "creadoPorId": str(n.creado_por_id) if n.creado_por_id else None, "createdAt": _d(n.created_at), "updatedAt": _d(n.updated_at)}


async def _cliente_publico(session, tenant_id: uuid.UUID, c: Cliente) -> dict[str, Any]:
    prestamos = await _prestamos_de(session, tenant_id, c.id, solo_vigentes=True)
    _, mora = await _saldo_y_mora(session, tenant_id, prestamos)
    return _cliente_gestor(c, len(prestamos), mora, await _foto(session, tenant_id, c.id))


@router.patch("/gestor/carteras/{cartera_id}")
async def editar_cartera(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    body = await request.json()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            c = await _cartera_visible(session, tenant_id, payload, cid)
            if c is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            if body.get("nombre"):
                c.nombre = str(body["nombre"]).strip()
            if "descripcion" in body:
                c.descripcion = body.get("descripcion") or None
    return JSONResponse({"id": str(cid), "nombre": c.nombre})


@router.patch("/gestor/carteras/{cartera_id}/configuracion")
async def editar_configuracion(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    body = await request.json()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            c = await _cartera_visible(session, tenant_id, payload, cid)
            if c is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            if body.get("tipoInteres") is not None:
                c.tipo_interes = Decimal(str(body["tipoInteres"]))
            if body.get("numCuotas") is not None:
                c.num_cuotas = int(body["numCuotas"])
    return JSONResponse({"id": str(cid), "tipoInteres": _f(c.tipo_interes), "numCuotas": c.num_cuotas})


@router.post("/gestor/carteras/{cartera_id}/notas")
async def crear_nota(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    body = await request.json()
    nota = str(body.get("nota") or "").strip()
    if not nota:
        return JSONResponse({"error": "nota vacía"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            n = CarteraNota(tenant_id=tenant_id, cartera_id=cid, nota=nota, creado_por_rol="gestor", creado_por_id=uuid.UUID(str(payload["admin_id"])) if payload.get("admin_id") else None)
            session.add(n)
            await session.flush()
            data = _nota_public(n)
    return JSONResponse(data, status_code=201)


@router.post("/gestor/carteras/{cartera_id}/apertura")
async def registrar_apertura(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    body = await request.json()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            ap = CarteraApertura(tenant_id=tenant_id, cartera_id=cid, fecha=date.today(), hora_inicio=datetime.now().strftime("%H:%M"), lat=body.get("latitud"), lng=body.get("longitud"))
            session.add(ap)
            await session.flush()
            out = {"id": str(ap.id), "carteraId": str(cid)}
    return JSONResponse(out, status_code=201)


@router.post("/gestor/carteras/{cartera_id}/posicion")
async def registrar_posicion(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    body = await request.json()
    try:
        lat, lng = float(body["latitud"]), float(body["longitud"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "coordenadas inválidas"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            gid = uuid.UUID(str(payload["gestor_id"])) if payload.get("gestor_id") else None
            session.add(PosicionGestor(tenant_id=tenant_id, gestor_id=gid, cartera_id=cid, lat=lat, lng=lng))
    return JSONResponse({"carteraId": str(cid), "latitud": lat, "longitud": lng}, status_code=201)


@router.post("/gestor/carteras/{cartera_id}/trayectoria-real")
async def trayectoria_real(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    body = await request.json()
    puntos = body.get("puntos") or []
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            log = CarteraOptimizadaLog(tenant_id=tenant_id, cartera_id=cid, fecha=date.today(), orden_clientes=[{"latitud": p.get("latitud"), "longitud": p.get("longitud")} for p in puntos], tipo="real")
            session.add(log)
            await session.flush()
            out = {"id": str(log.id), "tipo": "real"}
    return JSONResponse(out, status_code=201)


@router.post("/gestor/carteras/{cartera_id}/trayecto")
async def generar_trayecto(cartera_id: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    from pack_cobranza.admin import _ordenar_trayecto

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            clientes = (await session.execute(select(Cliente).where(Cliente.tenant_id == tenant_id, Cliente.cartera_id == cid, Cliente.deleted_at.is_(None)))).scalars().all()
            puntos = [{"id": str(c.id), "lat": c.lat, "lng": c.lng} for c in clientes if c.lat is not None and c.lng is not None]
            orden, km = _ordenar_trayecto(puntos, None)
            if not orden:
                return JSONResponse({"error": "sin clientes con ubicación"}, status_code=400)
            tiempo = int(km / 20 * 60) + len(orden) * 5
            log = CarteraOptimizadaLog(tenant_id=tenant_id, cartera_id=cid, fecha=date.today(), orden_clientes=orden, distancia_estimada_km=Decimal(str(round(km, 2))), tiempo_estimado_min=tiempo, tipo="planificada")
            session.add(log)
            await session.flush()
            por_id = {str(c.id): c for c in clientes}
            orden_pub = [{"clienteId": i, "latitud": por_id[i].lat, "longitud": por_id[i].lng} for i in orden]
            out = {"id": str(log.id), "carteraId": str(cid), "fecha": _d(log.fecha), "ordenClientes": [orden_pub], "waypoints": [{"latitud": o["latitud"], "longitud": o["longitud"]} for o in orden_pub], "distanciaEstimadaKm": float(round(km, 2)), "tiempoEstimadoMin": tiempo, "tipo": "planificada"}
    return JSONResponse(out, status_code=201)


def _num(v: Any) -> float | None:
    try:
        return float(str(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def _guardar_evidencias_cliente(session, tenant_id: uuid.UUID, cliente_id: uuid.UUID, form: Any) -> int:
    n = 0
    for campo in ("foto_facial", "documento_frente", "documento_reverso"):
        for archivo in form.getlist(campo):
            if not hasattr(archivo, "read"):
                continue
            contenido = await archivo.read()
            try:
                meta = await guardar_evidencia(tenant_id, cliente_id, campo, getattr(archivo, "filename", None), getattr(archivo, "content_type", None), contenido)
            except EvidenciaError:
                continue
            session.add(ClienteEvidencia(tenant_id=tenant_id, cliente_id=cliente_id, tipo=campo, **meta))  # type: ignore[arg-type]
            n += 1
    return n


@router.patch("/gestor/carteras/{cartera_id}/clientes/{cliente_id}")
async def editar_cliente(cartera_id: str, cliente_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid, clid = uuid.UUID(cartera_id), uuid.UUID(cliente_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    body = await request.json()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            c = await _cliente_o_404(session, tenant_id, cid, clid)
            if c is None:
                return JSONResponse({"error": "cliente no encontrado"}, status_code=404)
            for key, col in (("latitud", "lat"), ("longitud", "lng"), ("latitudDomicilio", "lat_domicilio"), ("longitudDomicilio", "lng_domicilio")):
                if key in body:
                    setattr(c, col, body.get(key))
            data = await _cliente_publico(session, tenant_id, c)
    return JSONResponse(data)


@router.post("/gestor/carteras/{cartera_id}/clientes")
async def crear_cliente(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    form = await request.form()
    nombre = str(form.get("nombre") or "").strip()
    if not nombre:
        return JSONResponse({"error": "nombre requerido"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            tope = _num(form.get("topeMaximoDeuda"))
            c = Cliente(
                tenant_id=tenant_id, cartera_id=cid, nombre=nombre, apellido=str(form.get("apellido") or "").strip() or None,
                telefono_whatsapp=telefono_para_guardar(form.get("telefonoWhatsapp")), negocio=str(form.get("negocio") or "").strip() or None,
                tipo_documento=str(form.get("tipoDocumento") or "").strip() or None, numero_documento=str(form.get("numeroDocumento") or "").strip() or None,
                lat=_num(form.get("latitud")), lng=_num(form.get("longitud")), lat_domicilio=_num(form.get("latitudDomicilio")), lng_domicilio=_num(form.get("longitudDomicilio")),
                tope_maximo_deuda=Decimal(str(tope)) if tope is not None else None,
            )
            session.add(c)
            await session.flush()
            await enlazar_cliente(session, c)
            await _guardar_evidencias_cliente(session, tenant_id, c.id, form)
            await session.flush()
            data = await _cliente_publico(session, tenant_id, c)
    return JSONResponse(data, status_code=201)


@router.post("/gestor/carteras/{cartera_id}/clientes/{cliente_id}/evidencias")
async def agregar_evidencias(cartera_id: str, cliente_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid, clid = uuid.UUID(cartera_id), uuid.UUID(cliente_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    form = await request.form()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            if await _cliente_o_404(session, tenant_id, cid, clid) is None:
                return JSONResponse({"error": "cliente no encontrado"}, status_code=404)
            await _guardar_evidencias_cliente(session, tenant_id, clid, form)
    return JSONResponse({"clienteId": str(clid)}, status_code=201)


async def _aplicar_visita(session, tenant_id: uuid.UUID, cid: uuid.UUID, data: dict[str, Any]) -> Visita:
    """Aplica una visita (pago o no-pago). Usado por el endpoint y el sync offline."""
    pid = uuid.UUID(str(data.get("prestamoId")))
    clid = uuid.UUID(str(data.get("clienteId")))
    if str(data.get("resultado") or "no_pago") == "pago":
        valor = Decimal(str(data.get("valor") or 0))
        if valor <= 0:
            raise ValueError("valor inválido")
        metodo = str(data.get("metodoPago") or "efectivo")
        tipo_pago = str(data.get("tipoPago") or "cuota")
        cuota_id = uuid.UUID(str(data["cuotaId"])) if data.get("cuotaId") else None
        visita = Visita(tenant_id=tenant_id, cartera_id=cid, cliente_id=clid, fecha=date.today(), resultado="pago", valor_pagado=valor, metodo_pago=metodo, creado_por="gestor")
        session.add(visita)
        if tipo_pago == "abono":
            session.add(Abono(tenant_id=tenant_id, prestamo_id=pid, cliente_id=clid, valor=valor, metodo_pago=metodo, registrado_por="gestor"))
        else:
            session.add(Pago(tenant_id=tenant_id, cliente_id=clid, prestamo_id=pid, cuota_id=cuota_id, valor=valor, metodo_pago=metodo, registrado_por="gestor"))
            if cuota_id:
                cuota = await get_owned(session, Cuota, cuota_id)
                if cuota is not None and cuota.prestamo_id == pid:
                    cuota.estatus = "pagada"
        p = await get_owned(session, Prestamo, pid)
        if p is not None:
            p.saldo = p.saldo - valor
            caja = await _caja(session, tenant_id, p.cartera_id)
            caja.saldo_actual = caja.saldo_actual + valor
        await session.flush()
        _auditar(session, tenant_id, "visita", visita.id, "crear", despues={"resultado": "pago", "valor": float(valor), "tipo": tipo_pago})
        return visita
    visita = Visita(tenant_id=tenant_id, cartera_id=cid, cliente_id=clid, fecha=date.today(), resultado="no_pago", motivo_no_pago=data.get("motivoNoPago"), creado_por="gestor")
    session.add(visita)
    if data.get("fechaPrometida") and data.get("valorPrometido"):
        session.add(Promesa(tenant_id=tenant_id, prestamo_id=pid, cliente_id=clid, fecha_prometida=date.fromisoformat(str(data["fechaPrometida"])), valor_prometido=Decimal(str(data["valorPrometido"])), creado_por="gestor"))
    await session.flush()
    _auditar(session, tenant_id, "visita", visita.id, "crear", despues={"resultado": "no_pago"})
    return visita


@router.post("/gestor/carteras/{cartera_id}/visitas/pago")
async def visita_pago(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    body = await request.json()
    try:
        pid = uuid.UUID(str(body.get("prestamoId")))
        clid = uuid.UUID(str(body.get("clienteId")))
    except (TypeError, ValueError):
        return JSONResponse({"error": "datos inválidos"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            p = await get_owned(session, Prestamo, pid)
            if p is None or p.cartera_id != cid or p.cliente_id != clid:
                return JSONResponse({"error": "préstamo no encontrado"}, status_code=404)
            try:
                visita = await _aplicar_visita(session, tenant_id, cid, body)
            except ValueError:
                return JSONResponse({"error": "datos inválidos"}, status_code=400)
            out = _visita_public(visita, pid)
    return JSONResponse(out, status_code=201)


@router.post("/gestor/carteras/{cartera_id}/visitas/no-pago")
async def visita_no_pago(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    body = await request.json()
    try:
        pid = uuid.UUID(str(body.get("prestamoId")))
    except (TypeError, ValueError):
        return JSONResponse({"error": "datos inválidos"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            visita = await _aplicar_visita(session, tenant_id, cid, {**body, "resultado": "no_pago"})
            out = _visita_public(visita, pid)
    return JSONResponse(out, status_code=201)


@router.post("/gestor/carteras/{cartera_id}/gastos")
async def registrar_gasto(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    form = await request.form()
    descripcion = str(form.get("descripcion") or "").strip()
    valor = _num(form.get("valor"))
    if not descripcion or valor is None:
        return JSONResponse({"error": "datos inválidos"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            g = Gasto(tenant_id=tenant_id, cartera_id=cid, descripcion=descripcion, valor=Decimal(str(valor)), creado_por=uuid.UUID(str(payload["admin_id"])) if payload.get("admin_id") else None)
            session.add(g)
            await session.flush()
            evidencias: list[GastoEvidencia] = []
            for archivo in form.getlist("evidencias"):
                if not hasattr(archivo, "read"):
                    continue
                contenido = await archivo.read()
                try:
                    meta = await guardar_archivo(tenant_id, str(g.id), getattr(archivo, "filename", None), getattr(archivo, "content_type", None), contenido)
                except EvidenciaError:
                    continue
                ev = GastoEvidencia(tenant_id=tenant_id, gasto_id=g.id, ruta_archivo=str(meta["ruta_archivo"]), nombre_original=meta["nombre_original"], mimetype=meta["mimetype"], tamano=meta["tamano"], creado_por_rol="gestor")  # type: ignore[arg-type]
                session.add(ev)
                evidencias.append(ev)
            await session.flush()
            data = _gasto_public(g, evidencias)
    return JSONResponse(data, status_code=201)


@router.post("/gestor/carteras/{cartera_id}/prestamos")
async def crear_prestamo(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    body = await request.json()
    try:
        clid = uuid.UUID(str(body.get("clienteId")))
        valor = Decimal(str(body.get("valor")))
        num_cuotas = int(body.get("numCuotas"))
        dias = int(body.get("diasEntreCuotas"))
        interes = Decimal(str(body.get("tipoInteres") or 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "datos inválidos"}, status_code=400)
    if valor <= 0 or num_cuotas < 1:
        return JSONResponse({"error": "datos inválidos"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            if await _cliente_o_404(session, tenant_id, cid, clid) is None:
                return JSONResponse({"error": "cliente no encontrado"}, status_code=404)
            total = (valor * (Decimal(1) + interes / Decimal(100))).quantize(Decimal("1"))
            cuota_valor = (total / num_cuotas).quantize(Decimal("1"))
            p = Prestamo(tenant_id=tenant_id, cliente_id=clid, cartera_id=cid, valor=valor, saldo=total, num_cuotas=num_cuotas, tipo_interes=interes, dias_entre_cuotas=dias, fecha_otorgado=date.today(), fiador_nombre=body.get("fiadorNombre"), fiador_apellido=body.get("fiadorApellido"), fiador_documento=body.get("fiadorDocumento"), fiador_telefono=body.get("fiadorTelefono"))
            session.add(p)
            await session.flush()
            for i in range(1, num_cuotas + 1):
                session.add(Cuota(tenant_id=tenant_id, prestamo_id=p.id, numero_cuota=i, valor_esperado=cuota_valor, fecha_vencimiento=date.today() + timedelta(days=i * dias)))
            caja = await _caja(session, tenant_id, cid)
            caja.saldo_actual = caja.saldo_actual - valor
            await session.flush()
            _auditar(session, tenant_id, "prestamo", p.id, "crear", despues={"valor": float(valor), "num_cuotas": num_cuotas})
            cuotas = (await session.execute(select(Cuota).where(Cuota.tenant_id == tenant_id, Cuota.prestamo_id == p.id).order_by(Cuota.numero_cuota))).scalars().all()
            data = _prestamo_public(p, list(cuotas))
    return JSONResponse(data, status_code=201)


@router.patch("/gestor/carteras/{cartera_id}/cuotas/{cuota_id}")
async def editar_cuota(cartera_id: str, cuota_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid, quid = uuid.UUID(cartera_id), uuid.UUID(cuota_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    body = await request.json()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            if not await _password_ok(session, payload, str(body.get("password") or "")):
                return JSONResponse({"error": "contraseña incorrecta"}, status_code=403)
            cuota = await get_owned(session, Cuota, quid)
            p = await get_owned(session, Prestamo, cuota.prestamo_id) if cuota else None
            if cuota is None or p is None or p.cartera_id != cid:
                return JSONResponse({"error": "cuota no encontrada"}, status_code=404)
            antes = {"valor": _f(cuota.valor_esperado)}
            if body.get("valorEsperado") is not None:
                cuota.valor_esperado = Decimal(str(body["valorEsperado"]))
            _auditar(session, tenant_id, "cuota", cuota.id, "editar", antes=antes, despues={"valor": _f(cuota.valor_esperado)}, motivo=body.get("motivo"))
            await session.flush()
            data = _cuota_public(cuota)
    return JSONResponse(data)


@router.delete("/gestor/carteras/{cartera_id}/cuotas/{cuota_id}")
async def eliminar_cuota(cartera_id: str, cuota_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid, quid = uuid.UUID(cartera_id), uuid.UUID(cuota_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    body = await request.json()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            if not await _password_ok(session, payload, str(body.get("password") or "")):
                return JSONResponse({"error": "contraseña incorrecta"}, status_code=403)
            cuota = await get_owned(session, Cuota, quid)
            p = await get_owned(session, Prestamo, cuota.prestamo_id) if cuota else None
            if cuota is None or p is None or p.cartera_id != cid:
                return JSONResponse({"error": "cuota no encontrada"}, status_code=404)
            cuota.deleted_at = datetime.now(UTC)
            _auditar(session, tenant_id, "cuota", cuota.id, "eliminar", antes={"valor": _f(cuota.valor_esperado)}, motivo=body.get("motivo"))
    return JSONResponse({"id": str(quid)})


async def _eliminar_movimiento(request: Request, authorization: str | None, cartera_id: str, record_id: str, modelo: Any, entidad: str) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid, rid = uuid.UUID(cartera_id), uuid.UUID(record_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    body = await request.json()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            if not await _password_ok(session, payload, str(body.get("password") or "")):
                return JSONResponse({"error": "contraseña incorrecta"}, status_code=403)
            obj = await get_owned(session, modelo, rid)
            if obj is None:
                return JSONResponse({"error": "no encontrado"}, status_code=404)
            obj.deleted_at = datetime.now(UTC)
            _auditar(session, tenant_id, entidad, obj.id, "eliminar", antes={"valor": _f(obj.valor)}, motivo=body.get("motivo"))
    return JSONResponse({"id": str(rid)})


@router.delete("/gestor/carteras/{cartera_id}/abonos/{record_id}")
async def eliminar_abono(cartera_id: str, record_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    return await _eliminar_movimiento(request, authorization, cartera_id, record_id, Abono, "abono")


@router.delete("/gestor/carteras/{cartera_id}/pagos/{record_id}")
async def eliminar_pago(cartera_id: str, record_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    return await _eliminar_movimiento(request, authorization, cartera_id, record_id, Pago, "pago")


def _liquidacion_public(liq: Liquidacion) -> dict[str, Any]:
    det = liq.detalle or {}
    return {
        "id": str(liq.id), "carteraId": str(liq.cartera_id), "fecha": _d(liq.fecha), "periodo": liq.periodo,
        "cajaAnterior": _f(liq.caja_anterior), "cajaActual": _f(liq.caja_actual),
        "estimadoACobrar": _f(liq.estimado_acobrar), "totalInyeccion": _f(liq.total_inyeccion),
        "totalCobradoPeriodo": _f(liq.total_cobrado), "totalCobradoDia": _f(liq.total_cobrado),
        "totalPrestado": _f(liq.total_prestado), "totalGastos": _f(liq.total_gastos),
        "sumaCartera": _f(liq.suma_cartera), "comisionPorcentaje": _f(liq.comision_porcentaje),
        "comisionValor": _f(liq.comision_valor), "comentario": liq.comentario,
        "createdAt": _d(liq.created_at), "pagos": [], "abonos": [],
        "pagaron": int(det.get("pagaron", 0)), "faltan": int(det.get("faltan", 0)),
        "totalDia": int(det.get("totalDia", 0)), "sinCuentas": int(det.get("sinCuentas", 0)),
        "horaPrimera": det.get("horaPrimera"), "horaUltima": det.get("horaUltima"),
    }


@router.post("/gestor/carteras/{cartera_id}/liquidaciones")
async def generar_liquidacion(cartera_id: str, request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid = uuid.UUID(cartera_id)
    except ValueError:
        return JSONResponse({"error": "cartera inválida"}, status_code=400)
    body = await request.json()
    from pack_cobranza.admin import _generar_liquidacion

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            cartera = await _cartera_visible(session, tenant_id, payload, cid)
            if cartera is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            cfg = (await session.execute(select(CarteraConfig).where(CarteraConfig.tenant_id == tenant_id, CarteraConfig.cartera_id == cid))).scalar_one_or_none()
            periodo = cfg.periodo_liquidacion if cfg is not None else "diario"
        await _generar_liquidacion(cid, periodo)
        async with get_session() as session:
            liq = (await session.execute(select(Liquidacion).where(Liquidacion.tenant_id == tenant_id, Liquidacion.cartera_id == cid, Liquidacion.deleted_at.is_(None)).order_by(Liquidacion.created_at.desc()).limit(1))).scalar_one_or_none()
            if liq is not None and body.get("comentario"):
                liq.comentario = str(body["comentario"])
            data = _liquidacion_public(liq) if liq is not None else None
    return JSONResponse(data, status_code=201)


@router.get("/gestor/carteras/{cartera_id}/liquidaciones/{liquidacion_id}/export-pdf")
async def exportar_liquidacion(cartera_id: str, liquidacion_id: str, authorization: str | None = Header(default=None)) -> Any:
    """v1: documento HTML imprimible (sin dependencia de PDF)."""
    from fastapi import Response

    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    try:
        cid, lid = uuid.UUID(cartera_id), uuid.UUID(liquidacion_id)
    except ValueError:
        return JSONResponse({"error": "id inválido"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            cartera = await get_owned(session, Cartera, cid)
            liq = await get_owned(session, Liquidacion, lid)
            if liq is None or liq.cartera_id != cid:
                return JSONResponse({"error": "liquidación no encontrada"}, status_code=404)
            html = (
                "<!doctype html><html><head><meta charset='utf-8'><title>Liquidación</title>"
                "<style>body{font-family:sans-serif;margin:2rem}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px 10px}</style>"
                f"</head><body><h1>Liquidación · {cartera.nombre if cartera else ''}</h1>"
                f"<p>Periodo: {liq.periodo} · Fecha: {_d(liq.fecha)}</p>"
                "<table><tr><th>Cobrado</th><th>Prestado</th><th>Gastos</th><th>Comisión</th></tr>"
                f"<tr><td>{_f(liq.total_cobrado):,.0f}</td><td>{_f(liq.total_prestado):,.0f}</td><td>{_f(liq.total_gastos):,.0f}</td><td>{_f(liq.comision_valor):,.0f}</td></tr></table>"
                "</body></html>"
            )
    return Response(content=html.encode("utf-8"), media_type="text/html")


# --------------------------------------------------------------- offline (F4)
def _ack(evento_id: str, estado: str, error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"eventoIdCliente": evento_id, "estado": estado}
    if error:
        out["error"] = error
    return out


async def _notificar_actividad_campo(tenant_id: uuid.UUID, payload: dict[str, Any], tipos_aplicados: list[str]) -> None:
    """Aviso al equipo cuando un GESTOR sincroniza eventos de campo (ADR-0099, ADR-0167).

    Solo si el autor es un gestor (un dueño operando su propia app no se
    auto-avisa) y se aplicó al menos un evento. Se agrupa por gestor y por media hora para no
    inundar el feed; un fallo del aviso nunca afecta el ack del sync.
    """
    if not tipos_aplicados or str(payload.get("rol") or "") != "gestor":
        return
    from datetime import UTC, datetime

    from core.notifications.service import notify_standalone
    from core.notifications.spec import NotificationSpec

    ahora = datetime.now(UTC)
    ventana = f"{ahora:%Y%m%d%H}{0 if ahora.minute < 30 else 1}"
    resumen = ", ".join(sorted(set(tipos_aplicados)))
    await notify_standalone(
        tenant_id,
        NotificationSpec(
            event_key="cobranza.actividad_de_campo",
            category="cobros",
            severity="info",
            title="Actividad de campo",
            body=f"Tu gestor sincronizó actividad del día: {resumen}.",
            dedupe_key=f"cobranza_sync:{payload.get('sub') or payload.get('gestorId') or 'gestor'}:{ventana}",
            section="pack_cobranza",
            data={"type": "cobranza.sync", "tipos": sorted(set(tipos_aplicados))},
            target={"entity": "cobranza", "id": None, "section": "pack_cobranza"},
        ),
    )


async def _cartera_de_evento(session, tenant_id: uuid.UUID, payload: dict[str, Any], data: dict[str, Any]) -> uuid.UUID | None:
    """Resuelve la cartera del evento: payload → cliente → única cartera visible."""
    if data.get("carteraId"):
        try:
            return uuid.UUID(str(data["carteraId"]))
        except ValueError:
            return None
    if data.get("clienteId"):
        try:
            cli = await get_owned(session, Cliente, uuid.UUID(str(data["clienteId"])))
        except ValueError:
            cli = None
        if cli is not None:
            return cli.cartera_id
    carteras = await _carteras_visibles(session, tenant_id, payload)
    return carteras[0].id if len(carteras) == 1 else None


async def _aplicar_evento_offline(session, tenant_id: uuid.UUID, payload: dict[str, Any], tipo: str, data: dict[str, Any]) -> None:
    if tipo in ("visita", "pago", "abono"):
        cid = await _cartera_de_evento(session, tenant_id, payload, data)
        if cid is None:
            raise ValueError("no se pudo resolver la cartera del evento")
        d = dict(data)
        if tipo == "pago":
            d["resultado"], d["tipoPago"] = "pago", "cuota"
        elif tipo == "abono":
            d["resultado"], d["tipoPago"] = "pago", "abono"
        await _aplicar_visita(session, tenant_id, cid, d)
    elif tipo == "gasto":
        cid = await _cartera_de_evento(session, tenant_id, payload, data)
        if cid is None:
            raise ValueError("no se pudo resolver la cartera del gasto")
        g = Gasto(tenant_id=tenant_id, cartera_id=cid, descripcion=str(data.get("descripcion") or "Gasto"), valor=Decimal(str(data.get("valor") or 0)), creado_por=uuid.UUID(str(payload["admin_id"])) if payload.get("admin_id") else None)
        session.add(g)
        await session.flush()
        for ev in data.get("evidencias") or []:
            b64 = ev.get("base64")
            if not b64:
                continue
            try:
                contenido = base64.b64decode(b64)
                meta = await guardar_archivo(tenant_id, str(g.id), ev.get("nombre"), ev.get("mimetype"), contenido)
            except (EvidenciaError, ValueError):
                continue
            session.add(GastoEvidencia(tenant_id=tenant_id, gasto_id=g.id, ruta_archivo=str(meta["ruta_archivo"]), nombre_original=meta["nombre_original"], mimetype=meta["mimetype"], tamano=meta["tamano"], creado_por_rol="gestor"))  # type: ignore[arg-type]
    elif tipo == "trayectoria":
        cid = await _cartera_de_evento(session, tenant_id, payload, data)
        if cid is None:
            raise ValueError("no se pudo resolver la cartera de la trayectoria")
        session.add(CarteraOptimizadaLog(tenant_id=tenant_id, cartera_id=cid, fecha=date.today(), orden_clientes=data.get("puntos") or [], tipo="real"))
    elif tipo == "cambio_cliente":
        clid = uuid.UUID(str(data.get("clienteId")))
        campos = data.get("input") or data.get("campos") or {}
        session.add(CambioCliente(tenant_id=tenant_id, cliente_id=clid, campos_propuestos=campos, estado="pendiente", solicitado_por_rol="gestor"))
    else:
        raise ValueError(f"tipo de evento no soportado: {tipo}")


@router.post("/sync-offline/eventos")
async def sync_offline(request: Request, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Sincroniza la cola offline del gestor; respuesta con ack por evento."""
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    body = await request.json()
    eventos = body.get("eventos") or []
    acks: list[dict[str, Any]] = []
    tipos_aplicados: list[str] = []
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            for ev in eventos:
                eid = str(ev.get("eventoIdCliente") or "").strip()
                tipo = str(ev.get("tipoEvento") or "").strip()
                data = ev.get("payload") or {}
                if not eid or not tipo:
                    acks.append(_ack(eid, "error", "evento inválido"))
                    continue
                existe = (await session.execute(select(SyncEvento.id).where(SyncEvento.tenant_id == tenant_id, SyncEvento.evento_id_cliente == eid))).scalar_one_or_none()
                if existe is not None:
                    acks.append(_ack(eid, "duplicado"))
                    continue
                try:
                    async with session.begin_nested():
                        await _aplicar_evento_offline(session, tenant_id, payload, tipo, data)
                    session.add(SyncEvento(tenant_id=tenant_id, evento_id_cliente=eid, tipo=tipo, payload=data))
                    acks.append(_ack(eid, "sincronizado"))
                    tipos_aplicados.append(tipo)
                except Exception as exc:  # evento inválido: no tumba el lote
                    session.add(SyncEvento(tenant_id=tenant_id, evento_id_cliente=eid, tipo=tipo, payload=data, estado="error"))
                    log.warning("sync_offline_error", tipo=tipo, error=type(exc).__name__)
                    acks.append(_ack(eid, "error", str(exc)))
    await _notificar_actividad_campo(tenant_id, payload, tipos_aplicados)
    return JSONResponse(acks)


@router.get("/sync-offline/trayecto-diario")
async def sync_trayecto(carteraId: str | None = None, authorization: str | None = Header(default=None)) -> JSONResponse:
    """Snapshot del trayecto del día para trabajar sin conexión."""
    auth = await _auth(authorization)
    if auth is None:
        return JSONResponse({"error": "no autorizado"}, status_code=401)
    tenant_id, payload = auth
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if carteraId:
                try:
                    cid = uuid.UUID(carteraId)
                except ValueError:
                    return JSONResponse({"error": "cartera inválida"}, status_code=400)
                if await _cartera_visible(session, tenant_id, payload, cid) is None:
                    return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            else:
                carteras = await _carteras_visibles(session, tenant_id, payload)
                if not carteras:
                    return JSONResponse({"clientes": [], "trayectos": None})
                cid = carteras[0].id
            data = await _trayecto_data(session, tenant_id, cid)
    return JSONResponse(data)
