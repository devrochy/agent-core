"""API de campo del gestor — contrato de la APK (ADR-0065).

Router separado del `api.py` de campo v1 (ADR-0057). Implementa el contrato exacto
de `app-cobranza-apk`: mismas rutas, **camelCase** idéntico a `types/gestor.ts` y
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

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from core.admin.auth import verify_password
from core.db.models import Admin, TenantUser
from core.db.session import get_owned, get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from pack_cobranza.auth import issue_token, pack_secret, token_tenant
from pack_cobranza.estadisticas import conteos_cartera
from pack_cobranza.evidencias import EvidenciaError, guardar_archivo, guardar_evidencia
from pack_cobranza.models import (
    Abono,
    AuditoriaCartera,
    Caja,
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
    Visita,
)
from pack_cobranza.permisos import GESTOR_PERMISOS

log = get_logger(__name__)

PLUGIN_ID = "pack_cobranza"
router = APIRouter(prefix="/api/pack/pack_cobranza", tags=[f"{PLUGIN_ID}_gestor"])

_ACCESS_TTL_H = 12
_REFRESH_TTL_H = 720  # 30 días


def _par_tokens(secret: str, tenant_id: uuid.UUID, admin_id: uuid.UUID, *, rol: str, gestor_id: uuid.UUID | None = None) -> dict[str, str]:
    base = {"tenant_id": str(tenant_id), "admin_id": str(admin_id), "rol": rol}
    if gestor_id is not None:
        base["gestor_id"] = str(gestor_id)
    return {
        "accessToken": issue_token({**base, "typ": "access"}, secret, ttl_hours=_ACCESS_TTL_H),
        "refreshToken": issue_token({**base, "typ": "refresh"}, secret, ttl_hours=_REFRESH_TTL_H),
    }


def _usuario_de(admin: Admin) -> str:
    return admin.username or admin.email


async def _auth(authorization: str | None) -> tuple[uuid.UUID, dict[str, Any]] | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return await token_tenant(authorization.split(" ", 1)[1].strip())


async def _admin_por_credenciales(session, usuario: str, password: str) -> Admin | None:
    admin = (
        await session.execute(
            select(Admin).where((Admin.email == usuario.lower()) | (Admin.username == usuario), Admin.active.is_(True))
        )
    ).scalar_one_or_none()
    if admin is None or not verify_password(password, admin.password_hash):
        return None
    return admin


@router.post("/auth/gestor/login")
async def gestor_login(request: Request) -> JSONResponse:
    body = await request.json()
    slug = str(body.get("tenant") or "").strip()
    usuario = str(body.get("usuario") or "").strip()
    password = str(body.get("password") or "")
    if not (slug and usuario and password):
        return JSONResponse({"error": "faltan datos"}, status_code=400)

    from core.tenants import resolve_tenant_by_slug

    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        return JSONResponse({"error": "tenant desconocido"}, status_code=401)

    async with tenant_scope(tenant_id):
        secret = await pack_secret()
        if not secret:
            return JSONResponse({"error": "pack sin secreto de firma"}, status_code=503)
        async with get_session() as session:
            admin = await _admin_por_credenciales(session, usuario, password)
            if admin is None:
                return JSONResponse({"error": "credenciales inválidas"}, status_code=401)
            if await _tenant_link(session, tenant_id, admin.id) is None:
                return JSONResponse({"error": "sin acceso a este tenant"}, status_code=403)
            gestor = (
                await session.execute(select(Gestor).where(Gestor.tenant_id == tenant_id, Gestor.admin_id == admin.id, Gestor.deleted_at.is_(None)))
            ).scalar_one_or_none()
            if gestor is None or gestor.estatus != "activo":
                return JSONResponse({"error": "no es gestor de esta cartera"}, status_code=403)
            tokens = _par_tokens(secret, tenant_id, admin.id, rol="gestor", gestor_id=gestor.id)
            perfil = {"id": str(gestor.id), "usuario": _usuario_de(admin), "nombre": gestor.nombre, "apellido": gestor.apellido or ""}
    return JSONResponse({**tokens, "gestor": perfil})


@router.post("/auth/propietario/login")
async def propietario_login(request: Request) -> JSONResponse:
    body = await request.json()
    slug = str(body.get("tenant") or "").strip()
    usuario = str(body.get("usuario") or "").strip()
    password = str(body.get("password") or "")
    if not (slug and usuario and password):
        return JSONResponse({"error": "faltan datos"}, status_code=400)

    from core.tenants import resolve_tenant_by_slug

    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        return JSONResponse({"error": "tenant desconocido"}, status_code=401)

    async with tenant_scope(tenant_id):
        secret = await pack_secret()
        if not secret:
            return JSONResponse({"error": "pack sin secreto de firma"}, status_code=503)
        async with get_session() as session:
            admin = await _admin_por_credenciales(session, usuario, password)
            if admin is None:
                return JSONResponse({"error": "credenciales inválidas"}, status_code=401)
            link = await _tenant_link(session, tenant_id, admin.id)
            if link is None or link.role != "owner":
                return JSONResponse({"error": "no es propietario de este tenant"}, status_code=403)
            tokens = _par_tokens(secret, tenant_id, admin.id, rol="propietario")
            perfil = {"id": str(admin.id), "usuario": _usuario_de(admin), "nombre": _usuario_de(admin), "apellido": ""}
    return JSONResponse({**tokens, "propietario": perfil})


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
            if admin is None:
                return JSONResponse({"error": "admin inactivo"}, status_code=401)
            gestor_id = uuid.UUID(str(payload["gestor_id"])) if payload.get("gestor_id") else None
            tokens = _par_tokens(secret, tenant_id, admin.id, rol=str(payload.get("rol") or "gestor"), gestor_id=gestor_id)
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


# --------------------------------------------------------------- lecturas (F2)
def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def _d(v: date | datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


def _hoy() -> date:
    return date.today()


async def _carteras_visibles(session, tenant_id: uuid.UUID, payload: dict[str, Any]) -> list[Cartera]:
    """Carteras que el token puede ver: todas para el propietario; las suyas para el gestor."""
    q = select(Cartera).where(Cartera.tenant_id == tenant_id, Cartera.deleted_at.is_(None)).order_by(Cartera.nombre)
    if payload.get("rol") != "propietario":
        gid = payload.get("gestor_id")
        if not gid:
            return []
        q = q.where(Cartera.gestor_id == uuid.UUID(str(gid)))
    return list((await session.execute(q)).scalars().all())


async def _cartera_visible(session, tenant_id: uuid.UUID, payload: dict[str, Any], cartera_id: uuid.UUID) -> Cartera | None:
    return next((c for c in await _carteras_visibles(session, tenant_id, payload) if c.id == cartera_id), None)


async def _permisos_de(session, tenant_id: uuid.UUID, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("rol") == "propietario":
        return [{"permiso": p, "habilitado": True} for p in GESTOR_PERMISOS]
    gid = payload.get("gestor_id")
    if not gid:
        return []
    rows = (await session.execute(select(GestorPermiso).where(GestorPermiso.tenant_id == tenant_id, GestorPermiso.gestor_id == uuid.UUID(str(gid))))).scalars().all()
    habilitados = {r.permiso: r.habilitado for r in rows}
    return [{"permiso": p, "habilitado": bool(habilitados.get(p, False))} for p in GESTOR_PERMISOS]


def _config_public(cartera_id: uuid.UUID, cfg: CarteraConfig | None) -> dict[str, Any]:
    def g(attr: str, default: Any) -> Any:
        return getattr(cfg, attr) if cfg is not None else default

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
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            permisos = await _permisos_de(session, tenant_id, payload)
            out = []
            for c in await _carteras_visibles(session, tenant_id, payload):
                cfg = (await session.execute(select(CarteraConfig).where(CarteraConfig.tenant_id == tenant_id, CarteraConfig.cartera_id == c.id))).scalar_one_or_none()
                out.append({"id": str(c.id), "nombre": c.nombre, "estatus": c.estatus, "tipoInteres": _f(c.tipo_interes), "numCuotas": c.num_cuotas, "config": _config_public(c.id, cfg), "permisos": permisos})
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
    hoy = _hoy()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
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
                out.append({"clienteId": str(c.id), "nombre": f"{c.nombre} {c.apellido or ''}".strip(), "enTrayecto": True, "color": color, "visitaRegistrada": visita_hoy is not None, "diasMora": mora, "compromisoValor": _f(promesa.valor_prometido) if promesa else None})
            log = (await session.execute(select(CarteraOptimizadaLog).where(CarteraOptimizadaLog.tenant_id == tenant_id, CarteraOptimizadaLog.cartera_id == cid).order_by(CarteraOptimizadaLog.created_at.desc()).limit(1))).scalar_one_or_none()
            trayecto = None
            if log is not None:
                por_id = {str(c.id): c for c in clientes}
                orden = [{"clienteId": cid_, "latitud": por_id[cid_].lat, "longitud": por_id[cid_].lng} for cid_ in (log.orden_clientes or []) if cid_ in por_id]
                trayecto = {"id": str(log.id), "carteraId": str(cid), "fecha": _d(log.fecha), "ordenClientes": orden, "waypoints": [{"latitud": p["latitud"], "longitud": p["longitud"]} for p in orden if p["latitud"] is not None and p["longitud"] is not None], "distanciaEstimadaKm": _f(log.distancia_estimada_km), "tiempoEstimadoMin": int(log.tiempo_estimado_min or 0), "tipo": log.tipo}
    return JSONResponse({"clientes": out, "trayectos": trayecto})


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
    return JSONResponse([
        {"id": str(liq.id), "carteraId": str(liq.cartera_id), "fecha": _d(liq.fecha), "periodo": liq.periodo,
         "cajaAnterior": _f(liq.caja_anterior), "cajaActual": _f(liq.caja_actual), "estimadoACobrar": 0.0,
         "totalInyeccion": 0.0, "totalCobradoPeriodo": _f(liq.total_cobrado), "totalCobradoDia": _f(liq.total_cobrado),
         "totalPrestado": _f(liq.total_prestado), "totalGastos": _f(liq.total_gastos), "sumaCartera": 0.0,
         "comisionPorcentaje": 0.0, "comisionValor": _f(liq.comision_valor), "comentario": liq.comentario,
         "createdAt": _d(liq.created_at), "pagos": [], "abonos": []}
        for liq in rows
    ])


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
    return admin is not None and verify_password(password, admin.password_hash)


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
            out = {"id": str(log.id), "carteraId": str(cid), "fecha": _d(log.fecha), "ordenClientes": orden_pub, "waypoints": [{"latitud": o["latitud"], "longitud": o["longitud"]} for o in orden_pub], "distanciaEstimadaKm": float(round(km, 2)), "tiempoEstimadoMin": tiempo, "tipo": "planificada"}
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
                telefono_whatsapp=str(form.get("telefonoWhatsapp") or "").strip() or None, negocio=str(form.get("negocio") or "").strip() or None,
                tipo_documento=str(form.get("tipoDocumento") or "").strip() or None, numero_documento=str(form.get("numeroDocumento") or "").strip() or None,
                lat=_num(form.get("latitud")), lng=_num(form.get("longitud")), lat_domicilio=_num(form.get("latitudDomicilio")), lng_domicilio=_num(form.get("longitudDomicilio")),
                tope_maximo_deuda=Decimal(str(tope)) if tope is not None else None,
            )
            session.add(c)
            await session.flush()
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
    valor = Decimal(str(body.get("valor") or 0))
    if valor <= 0:
        return JSONResponse({"error": "valor inválido"}, status_code=400)
    metodo = str(body.get("metodoPago") or "efectivo")
    tipo_pago = str(body.get("tipoPago") or "cuota")
    cuota_id = uuid.UUID(str(body["cuotaId"])) if body.get("cuotaId") else None
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            p = await get_owned(session, Prestamo, pid)
            if p is None or p.cartera_id != cid or p.cliente_id != clid:
                return JSONResponse({"error": "préstamo no encontrado"}, status_code=404)
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
            p.saldo = p.saldo - valor
            caja = await _caja(session, tenant_id, cid)
            caja.saldo_actual = caja.saldo_actual + valor
            await session.flush()
            _auditar(session, tenant_id, "visita", visita.id, "crear", despues={"resultado": "pago", "valor": float(valor), "tipo": tipo_pago})
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
        clid = uuid.UUID(str(body.get("clienteId")))
    except (TypeError, ValueError):
        return JSONResponse({"error": "datos inválidos"}, status_code=400)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if await _cartera_visible(session, tenant_id, payload, cid) is None:
                return JSONResponse({"error": "cartera no encontrada"}, status_code=404)
            visita = Visita(tenant_id=tenant_id, cartera_id=cid, cliente_id=clid, fecha=date.today(), resultado="no_pago", motivo_no_pago=body.get("motivoNoPago"), creado_por="gestor")
            session.add(visita)
            if body.get("fechaPrometida") and body.get("valorPrometido"):
                session.add(Promesa(tenant_id=tenant_id, prestamo_id=pid, cliente_id=clid, fecha_prometida=date.fromisoformat(str(body["fechaPrometida"])), valor_prometido=Decimal(str(body["valorPrometido"])), creado_por="gestor"))
            await session.flush()
            _auditar(session, tenant_id, "visita", visita.id, "crear", despues={"resultado": "no_pago"})
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
    return {"id": str(liq.id), "carteraId": str(liq.cartera_id), "fecha": _d(liq.fecha), "periodo": liq.periodo, "cajaAnterior": _f(liq.caja_anterior), "cajaActual": _f(liq.caja_actual), "estimadoACobrar": 0.0, "totalInyeccion": 0.0, "totalCobradoPeriodo": _f(liq.total_cobrado), "totalCobradoDia": _f(liq.total_cobrado), "totalPrestado": _f(liq.total_prestado), "totalGastos": _f(liq.total_gastos), "sumaCartera": 0.0, "comisionPorcentaje": 0.0, "comisionValor": _f(liq.comision_valor), "comentario": liq.comentario, "createdAt": _d(liq.created_at), "pagos": [], "abonos": []}


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
