"""Panel propio del pack de cobranza (`/cobranza/…`).

Navegación por secciones (ADR-0061). El **propietario es el tenant** (ADR-0062),
así que no hay CRUD de propietarios: se gestionan **Gestores, Carteras, Clientes**
y, dentro de cada cartera, Configuración / Clientes / Gastos / Inyecciones /
Liquidaciones.

Reutiliza el generador de formularios del core (`render_json_schema_form` /
`parse_json_schema_form`).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from core.admin.auth import current_admin, hash_password
from core.admin.context import base_context
from core.admin.form_generator import parse_json_schema_form, render_json_schema_form
from core.db.models import Conversation, Escalation
from core.db.session import get_owned, get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_cobranza.models import (
    Abono,
    AuditoriaCartera,
    Caja,
    CajaAjuste,
    CambioCliente,
    Cartera,
    CarteraApertura,
    CarteraConfig,
    CarteraEstadisticasSnapshot,
    CarteraNota,
    CarteraOptimizadaLog,
    Cliente,
    CobroPropietario,
    Cuota,
    Device,
    Gasto,
    Gestor,
    GestorPermiso,
    IntentoAcceso,
    Inyeccion,
    LinkPago,
    Liquidacion,
    Pago,
    PosicionGestor,
    Prestamo,
    Promesa,
    ReglaNegociacion,
    ReporteDiario,
    SyncEvento,
)
from pack_cobranza.permisos import GESTOR_PERMISOS

log = get_logger(__name__)

PLUGIN_ID = "pack_cobranza"
SECTION = PLUGIN_ID

router = APIRouter(prefix="/cobranza", tags=[PLUGIN_ID])
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _core_templates_dir() -> Path:
    import core

    return Path(core.__file__).resolve().parent / "admin" / "templates"


_templates.env.loader.searchpath.append(str(_core_templates_dir()))


def _render(name: str, ctx: dict[str, Any]):
    return _templates.TemplateResponse(ctx["request"], name, ctx)


def _uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


async def _guard(request: Request, section: str, *, write: bool) -> bool:
    from core.admin.permissions import require_section

    return await require_section(request, section, write=write)


def _request_section(request: Request) -> str:
    """Sub-sección del path (ADR-0063); fallback al `plugin_id`."""
    from core.admin.permissions import pack_prefixes, required_section
    from core.plugin_manager import plugin_manager

    req = required_section(request.url.path, request.method, pack_prefixes(plugin_manager.list_modules()))
    return req[0] if req else SECTION


async def _pack_enabled() -> bool:
    from core.agents.spec import enabled_packs, load_active_spec

    try:
        spec = await load_active_spec()
    except Exception:
        return False
    return PLUGIN_ID in enabled_packs(spec)


async def _authorized(request: Request, *, write: bool) -> bool:
    return await _guard(request, _request_section(request), write=write) and await _pack_enabled()


# --------------------------------------------------------------- config de cartera
_CONFIG_FIELDS: list[tuple[str, str, str, list[str]]] = [
    ("cuotas_minimas_prestamo", "Cuotas mínimas del préstamo", "int", []),
    ("cuotas_atraso_umbral", "Umbral de cuotas en atraso", "int", []),
    ("manejo_cupo_activo", "Manejo de cupo activo", "bool", []),
    ("cupo_default", "Cupo por defecto", "num", []),
    ("recargo_activo", "Recargo activo", "bool", []),
    ("bloquear_cambio_interes", "Bloquear cambio de interés", "bool", []),
    ("comision_activa", "Comisión activa", "bool", []),
    ("comision_porcentaje", "Comisión %", "num", []),
    ("mostrar_fecha_ultima_liquidada", "Mostrar fecha de última liquidación", "bool", []),
    ("mostrar_caja", "Mostrar caja", "bool", []),
    ("mostrar_cobrado_liquidada", "Mostrar cobrado liquidado", "bool", []),
    ("mostrar_prestamos", "Mostrar préstamos", "bool", []),
    ("mostrar_cobro_estimado", "Mostrar cobro estimado", "bool", []),
    ("ocultar_cartera", "Ocultar cartera", "bool", []),
    ("eliminar_prestamos_apk", "APK: eliminar préstamos", "bool", []),
    ("eliminar_pagos_apk", "APK: eliminar pagos", "bool", []),
    ("eliminar_abonos_apk", "APK: eliminar abonos", "bool", []),
    ("eliminar_gastos_apk", "APK: eliminar gastos", "bool", []),
    ("eliminar_inyeccion_apk", "APK: eliminar inyecciones", "bool", []),
    ("registrar_inyeccion_apk", "APK: registrar inyecciones", "bool", []),
    ("generar_reportes_apk", "APK: generar reportes", "bool", []),
    ("reconocimiento_facial_activo", "Reconocimiento facial", "bool", []),
    ("registro_documento_cliente", "Registro de documento del cliente", "bool", []),
    ("bloqueo_automatico_clientes", "Bloqueo automático de clientes", "bool", []),
    ("borrar_clientes_sin_deuda", "Borrar clientes sin deuda", "bool", []),
    ("permitir_cambio_fecha_prestamo", "Permitir cambio de fecha del préstamo", "bool", []),
    ("dias_no_laborables", "Días no laborables", "select", ["solo_domingos", "domingos_y_feriados"]),
    ("periodo_liquidacion", "Periodo de liquidación", "select", ["diario", "semanal", "quincenal", "mensual"]),
    ("dias_anticipacion_notificacion", "Días de anticipación de notificación", "int", []),
    ("aviso_dia_cobro", "Aviso el día de cobro", "bool", []),
    ("umbral_mora_notificacion", "Umbral de mora para notificar", "int", []),
    ("ruta_habilitada", "Ruta habilitada (feature)", "bool", []),
]

# --------------------------------------------------------------- esquemas de form
_SCHEMAS: dict[str, dict[str, Any]] = {
    "gestores": {
        "type": "object",
        "required": ["nombre"],
        "properties": {
            "nombre": {"type": "string", "title": "Nombre"},
            "apellido": {"type": "string", "title": "Apellido"},
            "correo": {"type": "string", "title": "Correo"},
            "telefono": {"type": "string", "title": "Teléfono"},
            "codigo": {"type": "string", "title": "Código"},
        },
    },
    "carteras": {
        "type": "object",
        "required": ["nombre"],
        "properties": {
            "nombre": {"type": "string", "title": "Nombre"},
            "gestor_id": {"type": "string", "title": "ID del gestor (opcional)"},
            "descripcion": {"type": "string", "title": "Descripción"},
            "tipo_interes": {"type": "number", "title": "Interés %"},
            "num_cuotas": {"type": "integer", "title": "Cuotas"},
            "moneda": {"type": "string", "title": "Moneda"},
            "costo_cobro": {"type": "number", "title": "Costo de cobro"},
        },
    },
    "clientes": {
        "type": "object",
        "required": ["nombre", "cartera_id"],
        "properties": {
            "nombre": {"type": "string", "title": "Nombre"},
            "apellido": {"type": "string", "title": "Apellido"},
            "telefono": {"type": "string", "title": "WhatsApp"},
            "tipo_documento": {"type": "string", "title": "Tipo de documento"},
            "numero_documento": {"type": "string", "title": "Número de documento"},
            "negocio": {"type": "string", "title": "Negocio"},
            "cartera_id": {"type": "string", "title": "ID de la cartera"},
            "tope_maximo_deuda": {"type": "number", "title": "Tope de deuda"},
            "color_riesgo": {"type": "string", "title": "Color de riesgo", "enum": ["blanco", "azul", "rojo"]},
        },
    },
    "gastos": {
        "type": "object",
        "required": ["cartera_id", "descripcion", "valor"],
        "properties": {
            "cartera_id": {"type": "string", "title": "ID de la cartera"},
            "descripcion": {"type": "string", "title": "Descripción"},
            "valor": {"type": "number", "title": "Valor"},
        },
    },
}

_TITLES = {"gestores": "Gestores", "carteras": "Carteras", "clientes": "Clientes", "gastos": "Gastos"}
_MODELS = {"gestores": Gestor, "carteras": Cartera, "clientes": Cliente, "gastos": Gasto}


# --------------------------------------------------------------- list/load/save
async def _rows(entity: str, *, cartera_id: uuid.UUID | None = None) -> tuple[list[dict[str, str]], list[tuple[str, str]]]:
    tid = get_current_tenant_id()
    async with get_session() as session:
        if entity == "gestores":
            data = (await session.execute(select(Gestor).where(Gestor.tenant_id == tid, Gestor.deleted_at.is_(None)))).scalars().all()
            rows = [{"_id": str(g.id), "nombre": f"{g.nombre} {g.apellido or ''}".strip(), "telefono": g.telefono or "", "codigo": g.codigo or "", "estatus": g.estatus} for g in data]
            cols = [("nombre", "Gestor"), ("telefono", "Teléfono"), ("codigo", "Código"), ("estatus", "Estatus")]
        elif entity == "carteras":
            data = (await session.execute(select(Cartera).where(Cartera.tenant_id == tid, Cartera.deleted_at.is_(None)))).scalars().all()
            gestores = {g.id: f"{g.nombre} {g.apellido or ''}".strip() for g in (await session.execute(select(Gestor).where(Gestor.tenant_id == tid))).scalars().all()}
            rows = [{"_id": str(c.id), "nombre": c.nombre, "gestor": gestores.get(c.gestor_id, "—") if c.gestor_id else "—", "interes": str(c.tipo_interes), "cuotas": str(c.num_cuotas), "moneda": c.moneda, "estatus": c.estatus} for c in data]
            cols = [("nombre", "Cartera"), ("gestor", "Gestor"), ("interes", "Interés %"), ("cuotas", "Cuotas"), ("moneda", "Moneda"), ("estatus", "Estatus")]
        elif entity == "clientes":
            q = select(Cliente).where(Cliente.tenant_id == tid, Cliente.deleted_at.is_(None))
            if cartera_id is not None:
                q = q.where(Cliente.cartera_id == cartera_id)
            data = (await session.execute(q.order_by(Cliente.nombre))).scalars().all()
            carteras = {c.id: c.nombre for c in (await session.execute(select(Cartera).where(Cartera.tenant_id == tid))).scalars().all()}
            rows = [
                {
                    "_id": str(c.id),
                    "nombre": f"{c.nombre} {c.apellido or ''}".strip(),
                    "telefono": c.telefono_whatsapp or "",
                    "documento": f"{c.tipo_documento or ''} {c.numero_documento or ''}".strip(),
                    "negocio": c.negocio or "",
                    "cartera": carteras.get(c.cartera_id, "—"),
                    "riesgo": c.color_riesgo,
                    "estatus": c.estatus,
                }
                for c in data
            ]
            cols = [("nombre", "Cliente"), ("telefono", "WhatsApp"), ("documento", "Documento"), ("negocio", "Negocio"), ("cartera", "Cartera"), ("riesgo", "Riesgo"), ("estatus", "Estatus")]
        else:  # gastos
            q = select(Gasto).where(Gasto.tenant_id == tid, Gasto.deleted_at.is_(None))
            if cartera_id is not None:
                q = q.where(Gasto.cartera_id == cartera_id)
            data = (await session.execute(q.order_by(Gasto.fecha_hora.desc()))).scalars().all()
            carteras = {c.id: c.nombre for c in (await session.execute(select(Cartera).where(Cartera.tenant_id == tid))).scalars().all()}
            rows = [{"_id": str(g.id), "descripcion": g.descripcion, "cartera": carteras.get(g.cartera_id, "—"), "valor": str(g.valor), "aprobado": "sí" if g.aprobado else "no", "fecha": g.fecha_hora.strftime("%Y-%m-%d") if g.fecha_hora else ""} for g in data]
            cols = [("descripcion", "Descripción"), ("cartera", "Cartera"), ("valor", "Valor"), ("aprobado", "Aprobado"), ("fecha", "Fecha")]
    return rows, cols


async def _load_values(entity: str, record_id: str) -> dict[str, Any] | None:
    rid = _uuid(record_id)
    if rid is None:
        return None
    async with get_session() as session:
        obj = await get_owned(session, _MODELS[entity], rid)
        if obj is None:
            return None
        if entity == "gestores":
            return {"nombre": obj.nombre, "apellido": obj.apellido, "correo": obj.correo, "telefono": obj.telefono, "codigo": obj.codigo}
        if entity == "carteras":
            return {"nombre": obj.nombre, "gestor_id": str(obj.gestor_id or ""), "descripcion": obj.descripcion, "tipo_interes": float(obj.tipo_interes), "num_cuotas": obj.num_cuotas, "moneda": obj.moneda, "costo_cobro": float(obj.costo_cobro)}
        if entity == "clientes":
            return {"nombre": obj.nombre, "apellido": obj.apellido, "telefono": obj.telefono_whatsapp, "tipo_documento": obj.tipo_documento, "numero_documento": obj.numero_documento, "negocio": obj.negocio, "cartera_id": str(obj.cartera_id), "tope_maximo_deuda": float(obj.tope_maximo_deuda or 0), "color_riesgo": obj.color_riesgo}
        return {"cartera_id": str(obj.cartera_id), "descripcion": obj.descripcion, "valor": float(obj.valor)}


async def _save(entity: str, record_id: str | None, values: dict[str, Any]) -> None:
    rid = _uuid(record_id)
    tid = get_current_tenant_id()
    async with get_session() as session:
        if entity == "gestores":
            obj = await get_owned(session, Gestor, rid) if rid else Gestor(tenant_id=tid)
            if rid and obj is None:
                return
            for f in ("nombre", "apellido", "correo", "telefono", "codigo"):
                if f in values:
                    setattr(obj, f, values[f])
            if rid is None:
                session.add(obj)
        elif entity == "carteras":
            gestor_id: uuid.UUID | None = None
            if values.get("gestor_id"):
                gestor_id = _uuid(str(values["gestor_id"]))
                if gestor_id is None or await get_owned(session, Gestor, gestor_id) is None:
                    return
            obj = await get_owned(session, Cartera, rid) if rid else Cartera(tenant_id=tid)
            if rid and obj is None:
                return
            obj.gestor_id = gestor_id
            for f in ("nombre", "descripcion", "tipo_interes", "num_cuotas", "moneda", "costo_cobro"):
                if f in values:
                    setattr(obj, f, values[f])
            if rid is None:
                session.add(obj)
                await session.flush()
                session.add(CarteraConfig(tenant_id=tid, cartera_id=obj.id))
        elif entity == "clientes":
            cartera_id = _uuid(str(values.get("cartera_id") or ""))
            if cartera_id is None or await get_owned(session, Cartera, cartera_id) is None:
                return
            obj = await get_owned(session, Cliente, rid) if rid else Cliente(tenant_id=tid)
            if rid and obj is None:
                return
            obj.cartera_id = cartera_id
            for f, col in (("nombre", "nombre"), ("apellido", "apellido"), ("telefono", "telefono_whatsapp"), ("tipo_documento", "tipo_documento"), ("numero_documento", "numero_documento"), ("negocio", "negocio"), ("tope_maximo_deuda", "tope_maximo_deuda"), ("color_riesgo", "color_riesgo")):
                if f in values:
                    setattr(obj, col, values[f])
            if rid is None:
                session.add(obj)
        else:  # gastos
            cartera_id = _uuid(str(values.get("cartera_id") or ""))
            if cartera_id is None or await get_owned(session, Cartera, cartera_id) is None:
                return
            descripcion = str(values.get("descripcion") or "").strip()
            if not descripcion or values.get("valor") is None:
                return
            if rid:
                gasto = await get_owned(session, Gasto, rid)
                if gasto is None:
                    return
                gasto.descripcion = descripcion
                gasto.valor = values["valor"]
                return
            session.add(Gasto(tenant_id=tid, cartera_id=cartera_id, descripcion=descripcion, valor=values["valor"], aprobado=False))


# --------------------------------------------------------------- resumen
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def resumen(request: Request) -> HTMLResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    tid = get_current_tenant_id()
    desde_semana = datetime.now(UTC) - timedelta(days=7)
    desde_mes = datetime.now(UTC) - timedelta(days=30)
    async with get_session() as session:
        carteras = (await session.execute(select(func.count()).select_from(Cartera).where(Cartera.tenant_id == tid, Cartera.deleted_at.is_(None)))).scalar_one()
        clientes = (await session.execute(select(func.count()).select_from(Cliente).where(Cliente.tenant_id == tid, Cliente.deleted_at.is_(None)))).scalar_one()
        prestamos = (await session.execute(select(func.count()).select_from(Prestamo).where(Prestamo.tenant_id == tid, Prestamo.estatus == "vigente", Prestamo.deleted_at.is_(None)))).scalar_one()
        saldo = (await session.execute(select(func.coalesce(func.sum(Cuota.valor_esperado), 0)).where(Cuota.tenant_id == tid, Cuota.estatus.in_(("pendiente", "atrasada")), Cuota.deleted_at.is_(None)))).scalar_one()
        mora = (await session.execute(select(func.coalesce(func.sum(Cuota.valor_esperado), 0)).where(Cuota.tenant_id == tid, Cuota.estatus == "atrasada", Cuota.deleted_at.is_(None)))).scalar_one()
        cobrado = (await session.execute(select(func.coalesce(func.sum(Pago.valor), 0)).where(Pago.tenant_id == tid, Pago.fecha_hora >= desde_semana, Pago.deleted_at.is_(None)))).scalar_one()
        gastos = (await session.execute(select(func.coalesce(func.sum(Gasto.valor), 0)).where(Gasto.tenant_id == tid, Gasto.fecha_hora >= desde_mes, Gasto.estado == "activo", Gasto.deleted_at.is_(None)))).scalar_one()

    def _fmt(v: Any) -> str:
        return f"{Decimal(v):,.0f}".replace(",", ".")

    ctx.update(
        kpis=[
            {"label": "Carteras", "value": carteras},
            {"label": "Clientes", "value": clientes},
            {"label": "Préstamos vigentes", "value": prestamos},
            {"label": "Saldo en cartera", "value": _fmt(saldo)},
            {"label": "Mora", "value": _fmt(mora)},
            {"label": "Cobrado (7 días)", "value": _fmt(cobrado)},
            {"label": "Gastos (30 días)", "value": _fmt(gastos)},
        ]
    )
    return _render("cobranza/resumen.html", ctx)


# --------------------------------------------------------------- reglas de negociación + monitoreo
@router.get("/reglas", response_class=HTMLResponse)
async def reglas_page(request: Request) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    tid = get_current_tenant_id()
    async with get_session() as session:
        r = (await session.execute(select(ReglaNegociacion).where(ReglaNegociacion.tenant_id == tid))).scalar_one_or_none()
        values = {
            "max_dias_prorroga": r.max_dias_prorroga if r else 15,
            "min_abono_pct": float(r.min_abono_pct) if r else 10.0,
            "max_reprogramaciones": r.max_reprogramaciones if r else 2,
            "umbral_saldo_autonomo": float(r.umbral_saldo_autonomo) if r else 0.0,
        }
    ctx.update(values=values)
    return _render("cobranza/reglas.html", ctx)


@router.post("/reglas")
async def reglas_save(request: Request) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    form = await request.form()
    tid = get_current_tenant_id()
    async with get_session() as session:
        r = (await session.execute(select(ReglaNegociacion).where(ReglaNegociacion.tenant_id == tid))).scalar_one_or_none()
        if r is None:
            r = ReglaNegociacion(tenant_id=tid)
            session.add(r)
        try:
            r.max_dias_prorroga = int(str(form.get("max_dias_prorroga") or 15))
            r.min_abono_pct = Decimal(str(form.get("min_abono_pct") or 10))
            r.max_reprogramaciones = int(str(form.get("max_reprogramaciones") or 2))
            r.umbral_saldo_autonomo = Decimal(str(form.get("umbral_saldo_autonomo") or 0))
        except Exception:
            return RedirectResponse("/cobranza/reglas?flash=Valores inválidos&flash_type=danger", status_code=303)
    return RedirectResponse("/cobranza/reglas?flash=Reglas guardadas&flash_type=success", status_code=303)


@router.get("/monitoreo", response_class=HTMLResponse)
async def monitoreo(request: Request) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    tid = get_current_tenant_id()
    async with get_session() as session:
        conversaciones = (await session.execute(select(func.count()).select_from(Conversation).where(Conversation.tenant_id == tid))).scalar_one()
        escalaciones = (await session.execute(select(func.count()).select_from(Escalation).where(Escalation.tenant_id == tid))).scalar_one()
        promesas = (await session.execute(select(Promesa).where(Promesa.tenant_id == tid, Promesa.deleted_at.is_(None)).order_by(Promesa.created_at.desc()).limit(20))).scalars().all()
        clientes = {c.id: f"{c.nombre} {c.apellido or ''}".strip() for c in (await session.execute(select(Cliente).where(Cliente.tenant_id == tid))).scalars().all()}
        pendientes = sum(1 for p in promesas if p.estado == "pendiente")
        cumplidas = sum(1 for p in promesas if p.estado == "cumplida")
        incumplidas = sum(1 for p in promesas if p.estado == "incumplida")
        ctx.update(
            conversaciones=conversaciones,
            escalaciones=escalaciones,
            promesas_pendientes=pendientes,
            promesas_cumplidas=cumplidas,
            promesas_incumplidas=incumplidas,
            promesas=[
                {"cliente": clientes.get(p.cliente_id, "—"), "fecha": p.fecha_prometida.isoformat(), "valor": float(p.valor_prometido), "estado": p.estado, "tipo": p.tipo, "origen": p.creado_por}
                for p in promesas
            ],
        )
    return _render("cobranza/monitoreo.html", ctx)


# --------------------------------------------------------------- cobros al propietario (Épica 9)
@router.get("/cobros", response_class=HTMLResponse)
async def cobros_page(request: Request) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    tid = get_current_tenant_id()
    async with get_session() as session:
        cobros = (await session.execute(select(CobroPropietario).where(CobroPropietario.tenant_id == tid, CobroPropietario.deleted_at.is_(None)).order_by(CobroPropietario.periodo.desc()))).scalars().all()
        links = (await session.execute(select(LinkPago).where(LinkPago.tenant_id == tid).order_by(LinkPago.created_at.desc()).limit(20))).scalars().all()
        ctx.update(
            cobros=[{"id": str(c.id), "periodo": c.periodo, "monto": float(c.monto_calculado), "pagado": float(c.monto_pagado) if c.monto_pagado is not None else None, "vence": c.fecha_vencimiento.isoformat(), "estado": c.estado} for c in cobros],
            links=[{"referencia": lk.referencia, "url": lk.url, "monto": float(lk.monto), "estado": lk.estado} for lk in links],
        )
    return _render("cobranza/cobros.html", ctx)


@router.post("/cobros/generar")
async def cobro_generar(request: Request) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    form = await request.form()
    periodo = str(form.get("periodo") or "").strip()
    try:
        monto = Decimal(str(form.get("monto") or 0))
        vence = date.fromisoformat(str(form.get("fecha_vencimiento") or ""))
    except Exception:
        return RedirectResponse("/cobranza/cobros?flash=Datos inválidos&flash_type=danger", status_code=303)
    if not periodo:
        return RedirectResponse("/cobranza/cobros?flash=Falta el periodo&flash_type=danger", status_code=303)
    tid = get_current_tenant_id()
    async with get_session() as session:
        existing = (await session.execute(select(CobroPropietario).where(CobroPropietario.tenant_id == tid, CobroPropietario.periodo == periodo))).scalar_one_or_none()
        if existing is None:
            session.add(CobroPropietario(tenant_id=tid, periodo=periodo, monto_calculado=monto, fecha_vencimiento=vence))
        else:
            existing.monto_calculado = monto
            existing.fecha_vencimiento = vence
    return RedirectResponse("/cobranza/cobros?flash=Cobro generado&flash_type=success", status_code=303)


@router.post("/cobros/{record_id}/pago")
async def cobro_pago(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    try:
        monto = Decimal(str(form.get("monto_pagado") or 0))
    except Exception:
        return RedirectResponse("/cobranza/cobros?flash=Monto inválido&flash_type=danger", status_code=303)
    if rid is not None:
        async with get_session() as session:
            cobro = await get_owned(session, CobroPropietario, rid)
            if cobro is not None:
                cobro.monto_pagado = monto
                cobro.fecha_pago = date.today()
                cobro.estado = "pagado"
                cobro.metodo_pago = str(form.get("metodo_pago") or "efectivo")
                cobro.registrado_por = "propietario"
    return RedirectResponse("/cobranza/cobros?flash=Pago registrado&flash_type=success", status_code=303)


@router.post("/cobros/{record_id}/link")
async def cobro_link(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    referencia = str(form.get("referencia") or "").strip()
    url = str(form.get("url") or "").strip()
    if rid is None or not referencia or not url:
        return RedirectResponse("/cobranza/cobros?flash=Referencia y URL obligatorias&flash_type=danger", status_code=303)
    tid = get_current_tenant_id()
    async with get_session() as session:
        cobro = await get_owned(session, CobroPropietario, rid)
        if cobro is None:
            return RedirectResponse("/cobranza/cobros?flash=Cobro no encontrado&flash_type=danger", status_code=303)
        session.add(LinkPago(tenant_id=tid, cobro_id=cobro.id, referencia=referencia, url=url, monto=cobro.monto_calculado))
    return RedirectResponse("/cobranza/cobros?flash=Link de pago creado&flash_type=success", status_code=303)


# --------------------------------------------------------------- dispositivos (ADR-0057)
async def _dispositivos_data(ctx: dict[str, Any]) -> None:
    tid = get_current_tenant_id()
    async with get_session() as session:
        devices = (await session.execute(select(Device).where(Device.tenant_id == tid, Device.deleted_at.is_(None)).order_by(Device.created_at.desc()))).scalars().all()
        gestores = {g.id: f"{g.nombre} {g.apellido or ''}".strip() for g in (await session.execute(select(Gestor).where(Gestor.tenant_id == tid))).scalars().all()}
        intentos = (await session.execute(select(IntentoAcceso).where(IntentoAcceso.tenant_id == tid).order_by(IntentoAcceso.created_at.desc()).limit(20))).scalars().all()
        eventos = (await session.execute(select(SyncEvento).where(SyncEvento.tenant_id == tid).order_by(SyncEvento.created_at.desc()).limit(50))).scalars().all()
        device_gestor = {d.id: (gestores.get(d.gestor_id, "—") if d.gestor_id else "—") for d in devices}
        ctx.update(
            dispositivos=[{"id": str(d.id), "codigo": d.codigo, "gestor": gestores.get(d.gestor_id, "—") if d.gestor_id else "—", "imei": d.imei or "", "estado": d.estado} for d in devices],
            gestores=[{"id": str(gid), "nombre": nombre} for gid, nombre in gestores.items()],
            intentos=[{"resultado": i.resultado, "motivo": i.motivo or "", "imei": i.imei or "", "fecha": i.created_at.strftime("%Y-%m-%d %H:%M") if i.created_at else ""} for i in intentos],
            eventos=[{"fecha": e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "", "tipo": e.tipo, "estado": e.estado, "gestor": device_gestor.get(e.device_id, "—") if e.device_id else "—", "evento": e.evento_id_cliente[:8], "critico": e.estado != "aplicado"} for e in eventos],
            eventos_error=sum(1 for e in eventos if e.estado != "aplicado"),
        )


@router.get("/dispositivos", response_class=HTMLResponse)
async def dispositivos(request: Request) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    await _dispositivos_data(ctx)
    return _render("cobranza/dispositivos.html", ctx)


@router.post("/dispositivos/generar", response_class=HTMLResponse)
async def dispositivo_generar(request: Request) -> HTMLResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    form = await request.form()
    gestor_id = _uuid(str(form.get("gestor_id") or ""))
    cartera_id = _uuid(str(form.get("cartera_id") or ""))
    codigo = str(uuid.uuid4())
    api_key = secrets.token_urlsafe(32)
    async with get_session() as session:
        if gestor_id is not None and await get_owned(session, Gestor, gestor_id) is None:
            gestor_id = None
        session.add(Device(tenant_id=get_current_tenant_id(), codigo=codigo, api_key_hash=hash_password(api_key), gestor_id=gestor_id, cartera_id=cartera_id, imei=str(form.get("imei") or "").strip() or None, whatsapp_number=str(form.get("whatsapp") or "").strip() or None))
    await _dispositivos_data(ctx)
    ctx.update(nuevo_codigo=codigo, nuevo_api_key=api_key)
    return _render("cobranza/dispositivos.html", ctx)


# --------------------------------------------------------------- reportes globales
@router.get("/reportes", response_class=HTMLResponse)
async def reportes(request: Request) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    tid = get_current_tenant_id()
    async with get_session() as session:
        carteras = (await session.execute(select(Cartera).where(Cartera.tenant_id == tid, Cartera.deleted_at.is_(None)).order_by(Cartera.nombre))).scalars().all()
        filas: list[dict[str, Any]] = []
        for c in carteras:
            n_clientes = (await session.execute(select(func.count()).select_from(Cliente).where(Cliente.tenant_id == tid, Cliente.cartera_id == c.id, Cliente.deleted_at.is_(None)))).scalar_one()
            prestamos = (await session.execute(select(Prestamo).where(Prestamo.tenant_id == tid, Prestamo.cartera_id == c.id, Prestamo.deleted_at.is_(None)))).scalars().all()
            saldo = sum((p.saldo for p in prestamos), Decimal(0))
            vencidas = (await session.execute(
                select(func.count()).select_from(Cuota).join(Prestamo, Cuota.prestamo_id == Prestamo.id).where(
                    Cuota.tenant_id == tid, Prestamo.cartera_id == c.id, Cuota.estatus != "pagada",
                    Cuota.fecha_vencimiento < date.today(), Cuota.deleted_at.is_(None),
                )
            )).scalar_one()
            cobrado = (await session.execute(
                select(func.coalesce(func.sum(Pago.valor), 0)).where(Pago.tenant_id == tid, Pago.cliente_id.in_([p.cliente_id for p in prestamos]) if prestamos else False, Pago.deleted_at.is_(None))
            )).scalar_one()
            gastos = (await session.execute(select(func.coalesce(func.sum(Gasto.valor), 0)).where(Gasto.tenant_id == tid, Gasto.cartera_id == c.id, Gasto.deleted_at.is_(None)))).scalar_one()
            caja = await _caja_de(session, c.id)
            filas.append({"cartera": c.nombre, "clientes": int(n_clientes), "prestamos": len(prestamos), "saldo": float(saldo), "vencidas": int(vencidas), "cobrado": float(cobrado), "caja": float(caja.saldo_actual), "gastos": float(gastos)})
        liquidaciones = (await session.execute(select(Liquidacion).where(Liquidacion.tenant_id == tid, Liquidacion.deleted_at.is_(None)).order_by(Liquidacion.fecha.desc()).limit(12))).scalars().all()
        cartera_nombre = {c.id: c.nombre for c in carteras}
        ctx.update(
            filas=filas,
            liquidaciones=[{"cartera": cartera_nombre.get(liq.cartera_id, "—"), "fecha": liq.fecha.isoformat(), "periodo": liq.periodo, "cobrado": float(liq.total_cobrado), "gastos": float(liq.total_gastos), "comision": float(liq.comision_valor)} for liq in liquidaciones],
            totales={
                "clientes": sum(f["clientes"] for f in filas),
                "saldo": sum(f["saldo"] for f in filas),
                "vencidas": sum(f["vencidas"] for f in filas),
                "cobrado": sum(f["cobrado"] for f in filas),
                "caja": sum(f["caja"] for f in filas),
                "gastos": sum(f["gastos"] for f in filas),
            },
        )
    return _render("cobranza/reportes.html", ctx)


# --------------------------------------------------------------- alertas de acceso
@router.get("/alertas", response_class=HTMLResponse)
async def alertas(request: Request) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    tid = get_current_tenant_id()
    async with get_session() as session:
        intentos = (await session.execute(select(IntentoAcceso).where(IntentoAcceso.tenant_id == tid).order_by(IntentoAcceso.created_at.desc()).limit(100))).scalars().all()
        gestores = {g.id: f"{g.nombre} {g.apellido or ''}".strip() for g in (await session.execute(select(Gestor).where(Gestor.tenant_id == tid))).scalars().all()}
        items = [
            {"resultado": i.resultado, "motivo": i.motivo or "", "imei": i.imei or "", "gestor": gestores.get(i.gestor_id, "—") if i.gestor_id else "—", "fecha": i.created_at.strftime("%Y-%m-%d %H:%M") if i.created_at else "", "critico": i.resultado != "ok"}
            for i in intentos
        ]
    ctx.update(alertas=items, total=len(items), denegados=sum(1 for i in items if i["critico"]))
    return _render("cobranza/alertas.html", ctx)


# --------------------------------------------------------------- editar cuota
_ESTATUS_CUOTA = ["pendiente", "pagada", "atrasada", "parcial"]


@router.get("/cuotas/{record_id}/editar", response_class=HTMLResponse)
async def cuota_editar(request: Request, record_id: str) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    async with get_session() as session:
        cuota = await get_owned(session, Cuota, rid) if rid else None
        if cuota is None:
            return RedirectResponse("/cobranza/clientes?flash=No encontrado&flash_type=danger", status_code=303)
        prestamo = await get_owned(session, Prestamo, cuota.prestamo_id)
        cliente_id = str(prestamo.cliente_id) if prestamo else ""
    ctx.update(cuota={"id": str(cuota.id), "numero": cuota.numero_cuota, "valor": float(cuota.valor_esperado), "vence": cuota.fecha_vencimiento.isoformat(), "estatus": cuota.estatus}, estatus=_ESTATUS_CUOTA, cliente_id=cliente_id)
    return _render("cobranza/cuota_form.html", ctx)


@router.post("/cuotas/{record_id}/editar")
async def cuota_editar_save(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    destino = f"/cobranza/cuotas/{record_id}/editar"
    try:
        valor = Decimal(str(form.get("valor") or 0))
        vence = date.fromisoformat(str(form.get("vence") or ""))
    except Exception:
        return RedirectResponse(f"{destino}?flash=Datos inválidos&flash_type=danger", status_code=303)
    estatus = str(form.get("estatus") or "pendiente")
    if estatus not in _ESTATUS_CUOTA:
        estatus = "pendiente"
    if rid is None or valor <= 0:
        return RedirectResponse(f"{destino}?flash=Datos inválidos&flash_type=danger", status_code=303)
    async with get_session() as session:
        cuota = await get_owned(session, Cuota, rid)
        if cuota is None:
            return RedirectResponse("/cobranza/clientes?flash=No encontrado&flash_type=danger", status_code=303)
        antes = {"valor": float(cuota.valor_esperado), "vence": cuota.fecha_vencimiento.isoformat(), "estatus": cuota.estatus}
        cuota.valor_esperado = valor
        cuota.fecha_vencimiento = vence
        cuota.estatus = estatus
        prestamo = await get_owned(session, Prestamo, cuota.prestamo_id)
        _auditar(session, "cuota", cuota.id, "editar", antes=antes, despues={"valor": float(valor), "vence": vence.isoformat(), "estatus": estatus})
        if prestamo is not None:
            destino = f"/cobranza/clientes/{prestamo.cliente_id}"
    return RedirectResponse(f"{destino}?flash=Cuota actualizada&flash_type=success", status_code=303)


# --------------------------------------------------------------- auditoría
@router.get("/auditoria", response_class=HTMLResponse)
async def auditoria(request: Request) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    qp = request.query_params
    f_entidad = (qp.get("entidad") or "").strip()
    f_operacion = (qp.get("operacion") or "").strip()
    f_desde = (qp.get("desde") or "").strip()
    f_hasta = (qp.get("hasta") or "").strip()
    tid = get_current_tenant_id()
    async with get_session() as session:
        q = select(AuditoriaCartera).where(AuditoriaCartera.tenant_id == tid)
        if f_entidad:
            q = q.where(AuditoriaCartera.entidad == f_entidad)
        if f_operacion:
            q = q.where(AuditoriaCartera.operacion == f_operacion)
        for valor, es_desde in ((f_desde, True), (f_hasta, False)):
            if not valor:
                continue
            try:
                dia = date.fromisoformat(valor)
            except ValueError:
                continue
            limite = datetime.combine(dia if es_desde else dia + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
            q = q.where(AuditoriaCartera.created_at >= limite) if es_desde else q.where(AuditoriaCartera.created_at < limite)
        filas = (await session.execute(q.order_by(AuditoriaCartera.created_at.desc()).limit(200))).scalars().all()
        entidades = sorted({e for (e,) in (await session.execute(select(AuditoriaCartera.entidad).where(AuditoriaCartera.tenant_id == tid).distinct())).all() if e})
        operaciones = sorted({o for (o,) in (await session.execute(select(AuditoriaCartera.operacion).where(AuditoriaCartera.tenant_id == tid).distinct())).all() if o})
    ctx.update(
        filas=[{"fecha": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "", "entidad": r.entidad, "id_corto": str(r.entidad_id)[:8], "operacion": r.operacion, "antes": r.valores_antes, "despues": r.valores_despues, "actor": r.actor_rol or "—", "motivo": r.motivo or ""} for r in filas],
        entidades=entidades, operaciones=operaciones, f_entidad=f_entidad, f_operacion=f_operacion, f_desde=f_desde, f_hasta=f_hasta,
    )
    return _render("cobranza/auditoria.html", ctx)


# --------------------------------------------------------------- promesas
_ESTADOS_PROMESA = ["pendiente", "cumplida", "incumplida"]


@router.get("/promesas", response_class=HTMLResponse)
async def promesas(request: Request) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    tid = get_current_tenant_id()
    f_estado = (request.query_params.get("estado") or "").strip()
    async with get_session() as session:
        q = select(Promesa).where(Promesa.tenant_id == tid, Promesa.deleted_at.is_(None))
        if f_estado:
            q = q.where(Promesa.estado == f_estado)
        filas = (await session.execute(q.order_by(Promesa.fecha_prometida.desc()).limit(200))).scalars().all()
        nombres = {c.id: f"{c.nombre} {c.apellido or ''}".strip() for c in (await session.execute(select(Cliente).where(Cliente.tenant_id == tid))).scalars().all()}
    ctx.update(
        promesas=[{"id": str(p.id), "cliente": nombres.get(p.cliente_id, "—"), "fecha": p.fecha_prometida.isoformat(), "valor": float(p.valor_prometido), "tipo": p.tipo, "estado": p.estado, "origen": p.creado_por, "vencida": p.estado == "pendiente" and p.fecha_prometida < date.today()} for p in filas],
        f_estado=f_estado, estados=_ESTADOS_PROMESA,
    )
    return _render("cobranza/promesas.html", ctx)


@router.post("/promesas/{record_id}/estado")
async def promesa_estado(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    nuevo = str(form.get("estado") or "")
    if nuevo not in _ESTADOS_PROMESA:
        return RedirectResponse("/cobranza/promesas?flash=Estado inválido&flash_type=danger", status_code=303)
    if rid is not None:
        async with get_session() as session:
            promesa = await get_owned(session, Promesa, rid)
            if promesa is not None:
                antes = promesa.estado
                promesa.estado = nuevo
                _auditar(session, "promesa", promesa.id, "editar", antes={"estado": antes}, despues={"estado": nuevo})
    return RedirectResponse("/cobranza/promesas?flash=Promesa actualizada&flash_type=success", status_code=303)


# --------------------------------------------------------------- caja
async def _caja_ledger(session, cartera_id: uuid.UUID) -> tuple[Caja, list[dict[str, Any]]]:
    """Saldo de la caja + libro de movimientos (cuadra con `saldo_actual`)."""
    tid = get_current_tenant_id()
    caja = await _caja_de(session, cartera_id)
    movs: list[dict[str, Any]] = []
    cliente_ids = [c.id for c in (await session.execute(select(Cliente).where(Cliente.tenant_id == tid, Cliente.cartera_id == cartera_id))).scalars().all()]
    if cliente_ids:
        for p in (await session.execute(select(Pago).where(Pago.tenant_id == tid, Pago.cliente_id.in_(cliente_ids), Pago.deleted_at.is_(None)))).scalars().all():
            movs.append({"fecha": p.fecha_hora, "tipo": "Pago", "signo": "+", "valor": float(p.valor), "detalle": p.metodo_pago or "efectivo"})
        for a in (await session.execute(select(Abono).where(Abono.tenant_id == tid, Abono.cliente_id.in_(cliente_ids), Abono.deleted_at.is_(None)))).scalars().all():
            movs.append({"fecha": a.fecha_hora, "tipo": "Abono", "signo": "+", "valor": float(a.valor), "detalle": a.metodo_pago or "efectivo"})
    for pr in (await session.execute(select(Prestamo).where(Prestamo.tenant_id == tid, Prestamo.cartera_id == cartera_id, Prestamo.deleted_at.is_(None)))).scalars().all():
        movs.append({"fecha": pr.created_at, "tipo": "Préstamo", "signo": "-", "valor": float(pr.valor), "detalle": "Desembolso"})
    for i in (await session.execute(select(Inyeccion).where(Inyeccion.tenant_id == tid, Inyeccion.cartera_id == cartera_id, Inyeccion.deleted_at.is_(None)))).scalars().all():
        movs.append({"fecha": i.fecha_hora, "tipo": "Inyección", "signo": "+", "valor": float(i.valor), "detalle": i.comentario or "Aporte de capital"})
    for g in (await session.execute(select(Gasto).where(Gasto.tenant_id == tid, Gasto.cartera_id == cartera_id, Gasto.deleted_at.is_(None), Gasto.aprobado.is_(True)))).scalars().all():
        movs.append({"fecha": g.created_at, "tipo": "Gasto", "signo": "-", "valor": float(g.valor), "detalle": g.descripcion or ""})
    for aj in (await session.execute(select(CajaAjuste).where(CajaAjuste.tenant_id == tid, CajaAjuste.caja_id == caja.id))).scalars().all():
        delta = float(aj.valor_nuevo) - float(aj.valor_anterior)
        movs.append({"fecha": aj.created_at, "tipo": "Ajuste", "signo": "+" if delta >= 0 else "-", "valor": abs(delta), "detalle": aj.motivo})
    movs = [m for m in movs if m["fecha"] is not None]
    movs.sort(key=lambda m: m["fecha"], reverse=True)
    for m in movs:
        m["fecha"] = m["fecha"].strftime("%Y-%m-%d %H:%M")
    return caja, movs


@router.get("/carteras/{record_id}/caja", response_class=HTMLResponse)
async def cartera_caja(request: Request, record_id: str) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    async with get_session() as session:
        cartera = await get_owned(session, Cartera, rid) if rid else None
        if cartera is None:
            return RedirectResponse("/cobranza/carteras?flash=No encontrada&flash_type=danger", status_code=303)
        caja, movimientos = await _caja_ledger(session, cartera.id)
        ctx.update(
            record_id=record_id, tab="caja", cartera=cartera.nombre,
            saldo_inicial=float(caja.saldo_inicial), saldo_actual=float(caja.saldo_actual),
            entradas=sum(m["valor"] for m in movimientos if m["signo"] == "+"),
            salidas=sum(m["valor"] for m in movimientos if m["signo"] == "-"),
            movimientos=movimientos,
        )
    return _render("cobranza/cartera_caja.html", ctx)


@router.post("/carteras/{record_id}/caja/ajuste")
async def caja_ajuste(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    destino = f"/cobranza/carteras/{record_id}/caja"
    form = await request.form()
    try:
        valor_nuevo = Decimal(str(form.get("valor_nuevo") or 0))
    except Exception:
        return RedirectResponse(f"{destino}?flash=Valor inválido&flash_type=danger", status_code=303)
    motivo = str(form.get("motivo") or "").strip()
    if rid is None or not motivo:
        return RedirectResponse(f"{destino}?flash=Indica el motivo del ajuste&flash_type=danger", status_code=303)
    async with get_session() as session:
        if await get_owned(session, Cartera, rid) is None:
            return RedirectResponse("/cobranza/carteras?flash=No encontrada&flash_type=danger", status_code=303)
        caja = await _caja_de(session, rid)
        anterior = caja.saldo_actual
        caja.saldo_actual = valor_nuevo
        session.add(CajaAjuste(tenant_id=get_current_tenant_id(), caja_id=caja.id, valor_anterior=anterior, valor_nuevo=valor_nuevo, motivo=motivo, actor_rol="admin"))
        _auditar(session, "caja", caja.id, "editar", antes={"saldo": float(anterior)}, despues={"saldo": float(valor_nuevo)}, motivo=motivo)
    return RedirectResponse(f"{destino}?flash=Caja ajustada&flash_type=success", status_code=303)


# --------------------------------------------------------------- CRUD genérico
@router.get("/{entity}", response_class=HTMLResponse)
async def entity_list(request: Request, entity: str) -> HTMLResponse:
    if entity not in _TITLES:
        return RedirectResponse("/admin", status_code=303)
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    rows, cols = await _rows(entity)
    ctx.update(entity=entity, title=_TITLES[entity], columns=cols, rows=rows)
    return _render("cobranza/lista.html", ctx)


@router.get("/{entity}/nuevo", response_class=HTMLResponse)
async def entity_new(request: Request, entity: str) -> HTMLResponse:
    if entity not in _TITLES:
        return RedirectResponse("/admin", status_code=303)
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    ctx.update(entity=entity, title=f"Nuevo · {_TITLES[entity]}", form_fields=render_json_schema_form(_SCHEMAS[entity]), record_id="")
    return _render("cobranza/form.html", ctx)


@router.get("/{entity}/{record_id}/editar", response_class=HTMLResponse)
async def entity_edit(request: Request, entity: str, record_id: str) -> HTMLResponse:
    if entity not in _TITLES:
        return RedirectResponse("/admin", status_code=303)
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    values = await _load_values(entity, record_id)
    if values is None:
        return RedirectResponse(f"/cobranza/{entity}?flash=No encontrado&flash_type=danger", status_code=303)
    ctx.update(entity=entity, title=f"Editar · {_TITLES[entity]}", form_fields=render_json_schema_form(_SCHEMAS[entity], values), record_id=record_id)
    return _render("cobranza/form.html", ctx)


@router.post("/{entity}/guardar")
async def entity_save(request: Request, entity: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if entity not in _TITLES or not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    form = await request.form()
    form_data = {str(k): str(v) for k, v in form.items()}
    record_id = form.get("record_id") or None
    values = parse_json_schema_form(_SCHEMAS[entity], form_data)
    await _save(entity, str(record_id) if record_id else None, values)
    return RedirectResponse(f"/cobranza/{entity}?flash=Guardado&flash_type=success", status_code=303)


@router.post("/{entity}/{record_id}/eliminar")
async def entity_delete(request: Request, entity: str, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if entity not in _TITLES or not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    if rid is not None:
        async with get_session() as session:
            obj = await get_owned(session, _MODELS[entity], rid)
            if obj is not None:
                obj.deleted_at = datetime.now(UTC)
                if hasattr(obj, "estado"):
                    obj.estado = "eliminado"
    return RedirectResponse(f"/cobranza/{entity}?flash=Eliminado&flash_type=success", status_code=303)


@router.post("/gastos/{record_id}/aprobar")
async def gasto_aprobar(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    if rid is not None:
        async with get_session() as session:
            gasto = await get_owned(session, Gasto, rid)
            if gasto is not None and not gasto.aprobado:
                gasto.aprobado = True
                caja = await _caja_de(session, gasto.cartera_id)
                caja.saldo_actual = caja.saldo_actual - gasto.valor
                _auditar(session, "gasto", gasto.id, "editar", antes={"aprobado": False}, despues={"aprobado": True}, motivo="Gasto aprobado")
    return RedirectResponse("/cobranza/gastos?flash=Gasto aprobado&flash_type=success", status_code=303)


# --------------------------------------------------------------- permisos de gestor
@router.get("/gestores/{record_id}/permisos", response_class=HTMLResponse)
async def gestor_permisos_page(request: Request, record_id: str) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    activos = await _gestor_permisos_activos(record_id)
    ctx.update(record_id=record_id, catalogo=GESTOR_PERMISOS, activos=activos)
    return _render("cobranza/permisos.html", ctx)


@router.post("/gestores/{record_id}/permisos")
async def gestor_permisos_save(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    if rid is None:
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    marcados = {str(v) for k, v in form.multi_items() if k == "permiso"}
    tid = get_current_tenant_id()
    async with get_session() as session:
        if await get_owned(session, Gestor, rid) is None:
            return RedirectResponse("/cobranza/gestores?flash=No encontrado&flash_type=danger", status_code=303)
        existing = (await session.execute(select(GestorPermiso).where(GestorPermiso.tenant_id == tid, GestorPermiso.gestor_id == rid))).scalars().all()
        by_permiso = {p.permiso: p for p in existing}
        for permiso in GESTOR_PERMISOS:
            row = by_permiso.get(permiso)
            if row is None:
                session.add(GestorPermiso(tenant_id=tid, gestor_id=rid, permiso=permiso, habilitado=permiso in marcados))
            else:
                row.habilitado = permiso in marcados
    return RedirectResponse(f"/cobranza/gestores/{record_id}/permisos?flash=Permisos guardados&flash_type=success", status_code=303)


async def _gestor_permisos_activos(record_id: str) -> set[str]:
    rid = _uuid(record_id)
    if rid is None:
        return set()
    tid = get_current_tenant_id()
    async with get_session() as session:
        rows = (await session.execute(select(GestorPermiso).where(GestorPermiso.tenant_id == tid, GestorPermiso.gestor_id == rid, GestorPermiso.habilitado.is_(True)))).scalars().all()
        return {r.permiso for r in rows}


# --------------------------------------------------------------- detalle de cartera
async def _cartera_or_redirect(rid: uuid.UUID | None):
    if rid is None:
        return None
    async with get_session() as session:
        return await get_owned(session, Cartera, rid)


@router.get("/carteras/{record_id}", response_class=HTMLResponse)
async def cartera_detalle(request: Request, record_id: str) -> HTMLResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    rid = _uuid(record_id)
    tid = get_current_tenant_id()
    async with get_session() as session:
        cartera = await get_owned(session, Cartera, rid) if rid else None
        if cartera is None:
            return RedirectResponse("/cobranza/carteras?flash=No encontrada&flash_type=danger", status_code=303)
        caja = await _caja_de(session, cartera.id)
        gastos = (await session.execute(select(Gasto).where(Gasto.tenant_id == tid, Gasto.cartera_id == cartera.id, Gasto.deleted_at.is_(None)).order_by(Gasto.fecha_hora.desc()).limit(10))).scalars().all()
        inyecciones = (await session.execute(select(Inyeccion).where(Inyeccion.tenant_id == tid, Inyeccion.cartera_id == cartera.id, Inyeccion.deleted_at.is_(None)).order_by(Inyeccion.fecha_hora.desc()).limit(10))).scalars().all()
        clientes = (await session.execute(select(func.count()).select_from(Cliente).where(Cliente.tenant_id == tid, Cliente.cartera_id == cartera.id, Cliente.deleted_at.is_(None)))).scalar_one()
        ctx.update(
            record_id=record_id, tab="resumen", cartera=cartera.nombre,
            saldo_inicial=float(caja.saldo_inicial), saldo_actual=float(caja.saldo_actual),
            clientes_count=clientes,
            gastos=[{"fecha": g.fecha_hora.strftime("%Y-%m-%d") if g.fecha_hora else "", "descripcion": g.descripcion, "valor": float(g.valor), "aprobado": g.aprobado} for g in gastos],
            inyecciones=[{"fecha": i.fecha_hora.strftime("%Y-%m-%d") if i.fecha_hora else "", "valor": float(i.valor), "comentario": i.comentario or ""} for i in inyecciones],
        )
    return _render("cobranza/cartera_detalle.html", ctx)


@router.get("/carteras/{record_id}/config", response_class=HTMLResponse)
async def cartera_config_page(request: Request, record_id: str) -> HTMLResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    rid = _uuid(record_id)
    tid = get_current_tenant_id()
    async with get_session() as session:
        cartera = await get_owned(session, Cartera, rid) if rid else None
        if cartera is None:
            return RedirectResponse("/cobranza/carteras?flash=No encontrada&flash_type=danger", status_code=303)
        cfg = (await session.execute(select(CarteraConfig).where(CarteraConfig.tenant_id == tid, CarteraConfig.cartera_id == rid))).scalar_one_or_none()
        values = {name: getattr(cfg, name) for name, *_ in _CONFIG_FIELDS} if cfg is not None else {}
    ctx.update(record_id=record_id, tab="config", cartera=cartera.nombre, fields=_CONFIG_FIELDS, values=values)
    return _render("cobranza/cartera_config.html", ctx)


@router.post("/carteras/{record_id}/config")
async def cartera_config_save(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    if rid is None:
        return RedirectResponse("/cobranza/carteras", status_code=303)
    form = await request.form()
    tid = get_current_tenant_id()
    async with get_session() as session:
        if await get_owned(session, Cartera, rid) is None:
            return RedirectResponse("/cobranza/carteras?flash=No encontrada&flash_type=danger", status_code=303)
        cfg = (await session.execute(select(CarteraConfig).where(CarteraConfig.tenant_id == tid, CarteraConfig.cartera_id == rid))).scalar_one_or_none()
        if cfg is None:
            cfg = CarteraConfig(tenant_id=tid, cartera_id=rid)
            session.add(cfg)
        for name, _label, typ, _opts in _CONFIG_FIELDS:
            if typ == "bool":
                setattr(cfg, name, name in form)
            elif typ == "int":
                try:
                    setattr(cfg, name, int(str(form.get(name) or 0)))
                except ValueError:
                    pass
            elif typ == "num":
                try:
                    setattr(cfg, name, Decimal(str(form.get(name) or 0)))
                except Exception:
                    pass
            else:
                val = str(form.get(name) or "").strip()
                if val:
                    setattr(cfg, name, val)
    return RedirectResponse(f"/cobranza/carteras/{record_id}/config?flash=Configuración guardada&flash_type=success", status_code=303)


@router.get("/carteras/{record_id}/clientes", response_class=HTMLResponse)
async def cartera_clientes(request: Request, record_id: str) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    rows, cols = await _rows("clientes", cartera_id=rid)
    ctx.update(record_id=record_id, tab="clientes", cartera=rid, columns=cols, rows=rows)
    return _render("cobranza/cartera_clientes.html", ctx)


@router.get("/carteras/{record_id}/gastos", response_class=HTMLResponse)
async def cartera_gastos(request: Request, record_id: str) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    rows, cols = await _rows("gastos", cartera_id=rid)
    ctx.update(record_id=record_id, tab="gastos", columns=cols, rows=rows)
    return _render("cobranza/cartera_gastos.html", ctx)


@router.get("/carteras/{record_id}/inyecciones", response_class=HTMLResponse)
async def cartera_inyecciones(request: Request, record_id: str) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    tid = get_current_tenant_id()
    async with get_session() as session:
        cartera = await get_owned(session, Cartera, rid) if rid else None
        if cartera is None:
            return RedirectResponse("/cobranza/carteras?flash=No encontrada&flash_type=danger", status_code=303)
        inyecciones = (await session.execute(select(Inyeccion).where(Inyeccion.tenant_id == tid, Inyeccion.cartera_id == cartera.id, Inyeccion.deleted_at.is_(None)).order_by(Inyeccion.fecha_hora.desc()))).scalars().all()
        ctx.update(record_id=record_id, tab="inyecciones", cartera=cartera.nombre, inyecciones=[{"fecha": i.fecha_hora.strftime("%Y-%m-%d %H:%M") if i.fecha_hora else "", "valor": float(i.valor), "comentario": i.comentario or ""} for i in inyecciones])
    return _render("cobranza/cartera_inyecciones.html", ctx)


@router.get("/carteras/{record_id}/liquidaciones", response_class=HTMLResponse)
async def cartera_liquidaciones(request: Request, record_id: str) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    tid = get_current_tenant_id()
    async with get_session() as session:
        cartera = await get_owned(session, Cartera, rid) if rid else None
        if cartera is None:
            return RedirectResponse("/cobranza/carteras?flash=No encontrada&flash_type=danger", status_code=303)
        liquidaciones = (await session.execute(select(Liquidacion).where(Liquidacion.tenant_id == tid, Liquidacion.cartera_id == cartera.id, Liquidacion.deleted_at.is_(None)).order_by(Liquidacion.created_at.desc()))).scalars().all()
        ctx.update(record_id=record_id, tab="liquidaciones", cartera=cartera.nombre, liquidaciones=[{"fecha": liq.fecha.isoformat(), "periodo": liq.periodo, "cobrado": float(liq.total_cobrado), "gastos": float(liq.total_gastos), "comision": float(liq.comision_valor)} for liq in liquidaciones])
    return _render("cobranza/cartera_liquidaciones.html", ctx)


@router.post("/carteras/{record_id}/inyeccion")
async def cartera_inyeccion(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    try:
        valor = Decimal(str(form.get("valor") or 0))
    except Exception:
        return RedirectResponse(f"/cobranza/carteras/{record_id}/inyecciones?flash=Valor inválido&flash_type=danger", status_code=303)
    if rid is not None and valor > 0:
        async with get_session() as session:
            if await get_owned(session, Cartera, rid) is not None:
                inyeccion = Inyeccion(tenant_id=get_current_tenant_id(), cartera_id=rid, valor=valor, comentario=str(form.get("comentario") or "").strip() or None)
                session.add(inyeccion)
                await session.flush()
                caja = await _caja_de(session, rid)
                caja.saldo_actual = caja.saldo_actual + valor
                _auditar(session, "inyeccion", inyeccion.id, "crear", despues={"valor": float(valor)}, motivo="Aporte de capital")
    return RedirectResponse(f"/cobranza/carteras/{record_id}/inyecciones?flash=Inyección registrada&flash_type=success", status_code=303)


@router.post("/carteras/{record_id}/liquidacion")
async def cartera_liquidacion(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    periodo = str(form.get("periodo") or "diario")
    if rid is not None:
        await _generar_liquidacion(rid, periodo)
    return RedirectResponse(f"/cobranza/carteras/{record_id}/liquidaciones?flash=Liquidación generada&flash_type=success", status_code=303)


# --------------------------------------------------------------- cliente: detalle y operaciones
def _auditar(session, entidad: str, entidad_id: uuid.UUID, operacion: str, *, antes=None, despues=None, motivo=None) -> None:
    session.add(
        AuditoriaCartera(
            tenant_id=get_current_tenant_id(),
            entidad=entidad,
            entidad_id=entidad_id,
            operacion=operacion,
            valores_antes=antes,
            valores_despues=despues,
            actor_rol="admin",
            motivo=motivo,
        )
    )


@router.get("/clientes/{record_id}", response_class=HTMLResponse)
async def cliente_detalle(request: Request, record_id: str) -> HTMLResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    rid = _uuid(record_id)
    tid = get_current_tenant_id()
    async with get_session() as session:
        cliente = await get_owned(session, Cliente, rid) if rid else None
        if cliente is None:
            return RedirectResponse("/cobranza/clientes?flash=No encontrado&flash_type=danger", status_code=303)
        cartera = await get_owned(session, Cartera, cliente.cartera_id)
        prestamos = (
            await session.execute(
                select(Prestamo).where(Prestamo.tenant_id == tid, Prestamo.cliente_id == cliente.id, Prestamo.deleted_at.is_(None)).order_by(Prestamo.created_at.desc())
            )
        ).scalars().all()
        prestamos_data = []
        for p in prestamos:
            cuotas = (
                await session.execute(
                    select(Cuota).where(Cuota.tenant_id == tid, Cuota.prestamo_id == p.id, Cuota.deleted_at.is_(None)).order_by(Cuota.numero_cuota)
                )
            ).scalars().all()
            prestamos_data.append(
                {
                    "id": str(p.id),
                    "valor": float(p.valor),
                    "saldo": float(p.saldo),
                    "num_cuotas": p.num_cuotas,
                    "estatus": p.estatus,
                    "fecha": p.fecha_otorgado.isoformat(),
                    "cuotas": [
                        {"id": str(c.id), "numero": c.numero_cuota, "valor": float(c.valor_esperado), "vence": c.fecha_vencimiento.isoformat(), "estatus": c.estatus}
                        for c in cuotas
                    ],
                }
            )
        abonos = (
            await session.execute(
                select(Abono).where(Abono.tenant_id == tid, Abono.cliente_id == cliente.id, Abono.deleted_at.is_(None)).order_by(Abono.fecha_hora.desc())
            )
        ).scalars().all()
        cambios = (
            await session.execute(
                select(CambioCliente).where(CambioCliente.tenant_id == tid, CambioCliente.cliente_id == cliente.id, CambioCliente.estado == "pendiente")
            )
        ).scalars().all()
        conv_id = None
        if cliente.customer_id is not None:
            conv = (await session.execute(select(Conversation).where(Conversation.tenant_id == tid, Conversation.customer_id == cliente.customer_id).order_by(Conversation.started_at.desc()).limit(1))).scalar_one_or_none()
            conv_id = str(conv.id) if conv else None
        ctx.update(
            record_id=record_id,
            cliente=f"{cliente.nombre} {cliente.apellido or ''}".strip(),
            telefono=cliente.telefono_whatsapp or "",
            documento=f"{cliente.tipo_documento or ''} {cliente.numero_documento or ''}".strip(),
            negocio=cliente.negocio or "",
            riesgo=cliente.color_riesgo,
            cartera=cartera.nombre if cartera else "—",
            customer_id=str(cliente.customer_id) if cliente.customer_id else "",
            conv_id=conv_id,
            prestamos=prestamos_data,
            abonos=[{"id": str(a.id), "valor": float(a.valor), "metodo": a.metodo_pago, "fecha": a.fecha_hora.strftime("%Y-%m-%d") if a.fecha_hora else ""} for a in abonos],
            cambios=[{"id": str(c.id), "campos": c.campos_propuestos, "fecha": c.created_at.strftime("%Y-%m-%d") if c.created_at else ""} for c in cambios],
        )
    return _render("cobranza/cliente_detalle.html", ctx)


@router.post("/clientes/{record_id}/prestamo")
async def cliente_prestamo(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    try:
        valor = Decimal(str(form.get("valor") or 0))
        num_cuotas = int(str(form.get("num_cuotas") or 1))
        tipo_interes = Decimal(str(form.get("tipo_interes") or 0))
        dias = int(str(form.get("dias_entre_cuotas") or 1))
        fecha = date.fromisoformat(str(form.get("fecha_otorgado") or date.today().isoformat()))
    except Exception:
        return RedirectResponse(f"/cobranza/clientes/{record_id}?flash=Datos inválidos&flash_type=danger", status_code=303)
    if rid is None or valor <= 0 or num_cuotas < 1:
        return RedirectResponse(f"/cobranza/clientes/{record_id}?flash=Datos inválidos&flash_type=danger", status_code=303)
    tid = get_current_tenant_id()
    async with get_session() as session:
        cliente = await get_owned(session, Cliente, rid)
        if cliente is None:
            return RedirectResponse("/cobranza/clientes?flash=No encontrado&flash_type=danger", status_code=303)
        total = (valor * (Decimal(1) + tipo_interes / Decimal(100))).quantize(Decimal("1"))
        cuota_valor = (total / num_cuotas).quantize(Decimal("1"))
        prestamo = Prestamo(tenant_id=tid, cliente_id=cliente.id, cartera_id=cliente.cartera_id, valor=valor, saldo=total, num_cuotas=num_cuotas, tipo_interes=tipo_interes, dias_entre_cuotas=dias, fecha_otorgado=fecha)
        session.add(prestamo)
        await session.flush()
        for i in range(1, num_cuotas + 1):
            session.add(Cuota(tenant_id=tid, prestamo_id=prestamo.id, numero_cuota=i, valor_esperado=cuota_valor, fecha_vencimiento=fecha + timedelta(days=i * dias)))
        caja = await _caja_de(session, cliente.cartera_id)
        caja.saldo_actual = caja.saldo_actual - valor
        _auditar(session, "prestamo", prestamo.id, "crear", despues={"valor": float(valor), "num_cuotas": num_cuotas, "cliente_id": str(cliente.id)})
    return RedirectResponse(f"/cobranza/clientes/{record_id}?flash=Préstamo creado&flash_type=success", status_code=303)


@router.post("/prestamos/{record_id}/pago")
async def prestamo_pago(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    try:
        valor = Decimal(str(form.get("valor") or 0))
    except Exception:
        return RedirectResponse("/cobranza/clientes?flash=Valor inválido&flash_type=danger", status_code=303)
    metodo = str(form.get("metodo_pago") or "efectivo")
    cuota_id = _uuid(str(form.get("cuota_id") or ""))
    if rid is None or valor <= 0:
        return RedirectResponse("/cobranza/clientes?flash=Valor inválido&flash_type=danger", status_code=303)
    tid = get_current_tenant_id()
    async with get_session() as session:
        prestamo = await get_owned(session, Prestamo, rid)
        if prestamo is None:
            return RedirectResponse("/cobranza/clientes?flash=Préstamo no encontrado&flash_type=danger", status_code=303)
        cuota = await get_owned(session, Cuota, cuota_id) if cuota_id else None
        session.add(Pago(tenant_id=tid, cliente_id=prestamo.cliente_id, prestamo_id=prestamo.id, cuota_id=cuota.id if cuota else None, valor=valor, metodo_pago=metodo, registrado_por="propietario"))
        if cuota is not None and cuota.prestamo_id == prestamo.id:
            cuota.estatus = "pagada"
        prestamo.saldo = prestamo.saldo - valor
        caja = await _caja_de(session, prestamo.cartera_id)
        caja.saldo_actual = caja.saldo_actual + valor
        _auditar(session, "pago", prestamo.id, "crear", despues={"valor": float(valor), "metodo": metodo, "cuota_id": str(cuota.id) if cuota else None})
        cliente_id = str(prestamo.cliente_id)
    return RedirectResponse(f"/cobranza/clientes/{cliente_id}?flash=Pago registrado&flash_type=success", status_code=303)


@router.post("/prestamos/{record_id}/abono")
async def prestamo_abono(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    try:
        valor = Decimal(str(form.get("valor") or 0))
    except Exception:
        return RedirectResponse("/cobranza/clientes?flash=Valor inválido&flash_type=danger", status_code=303)
    metodo = str(form.get("metodo_pago") or "efectivo")
    if rid is None or valor <= 0:
        return RedirectResponse("/cobranza/clientes?flash=Valor inválido&flash_type=danger", status_code=303)
    tid = get_current_tenant_id()
    async with get_session() as session:
        prestamo = await get_owned(session, Prestamo, rid)
        if prestamo is None:
            return RedirectResponse("/cobranza/clientes?flash=Préstamo no encontrado&flash_type=danger", status_code=303)
        session.add(Abono(tenant_id=tid, prestamo_id=prestamo.id, cliente_id=prestamo.cliente_id, valor=valor, metodo_pago=metodo, registrado_por="propietario"))
        prestamo.saldo = prestamo.saldo - valor
        caja = await _caja_de(session, prestamo.cartera_id)
        caja.saldo_actual = caja.saldo_actual + valor
        _auditar(session, "abono", prestamo.id, "crear", despues={"valor": float(valor), "metodo": metodo})
        cliente_id = str(prestamo.cliente_id)
    return RedirectResponse(f"/cobranza/clientes/{cliente_id}?flash=Abono registrado&flash_type=success", status_code=303)


@router.post("/cuotas/{record_id}/eliminar")
async def cuota_eliminar(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    destino = "/cobranza/clientes"
    if rid is not None:
        async with get_session() as session:
            cuota = await get_owned(session, Cuota, rid)
            if cuota is not None:
                cuota.deleted_at = datetime.now(UTC)
                prestamo = await get_owned(session, Prestamo, cuota.prestamo_id)
                if prestamo is not None:
                    destino = f"/cobranza/clientes/{prestamo.cliente_id}"
                _auditar(session, "cuota", cuota.id, "eliminar", antes={"valor": float(cuota.valor_esperado), "numero": cuota.numero_cuota})
    return RedirectResponse(f"{destino}?flash=Cuota eliminada&flash_type=success", status_code=303)


@router.post("/abonos/{record_id}/eliminar")
async def abono_eliminar(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    destino = "/cobranza/clientes"
    if rid is not None:
        async with get_session() as session:
            abono = await get_owned(session, Abono, rid)
            if abono is not None:
                abono.deleted_at = datetime.now(UTC)
                destino = f"/cobranza/clientes/{abono.cliente_id}"
                _auditar(session, "abono", abono.id, "eliminar", antes={"valor": float(abono.valor)})
    return RedirectResponse(f"{destino}?flash=Abono eliminado&flash_type=success", status_code=303)


@router.post("/cambios/{record_id}/decision")
async def cambio_decision(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    decision = str(form.get("decision") or "")
    destino = "/cobranza/clientes"
    if rid is not None:
        async with get_session() as session:
            cambio = await get_owned(session, CambioCliente, rid)
            if cambio is not None:
                destino = f"/cobranza/clientes/{cambio.cliente_id}"
                if decision == "aprobar":
                    cliente = await get_owned(session, Cliente, cambio.cliente_id)
                    if cliente is not None:
                        for field, value in (cambio.campos_propuestos or {}).items():
                            if field in ("nombre", "apellido", "negocio", "telefono_whatsapp", "tipo_documento", "numero_documento") and value is not None:
                                setattr(cliente, field, str(value))
                    cambio.estado = "aprobado"
                else:
                    cambio.estado = "rechazado"
                    cambio.motivo_rechazo = str(form.get("motivo") or "").strip() or None
                cambio.revisado_en = datetime.now(UTC)
    return RedirectResponse(f"{destino}?flash=Cambio {decision or 'procesado'}&flash_type=success", status_code=303)


# --------------------------------------------------------------- cartera: notas / estadísticas / trayecto
@router.get("/carteras/{record_id}/notas", response_class=HTMLResponse)
async def cartera_notas(request: Request, record_id: str) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    tid = get_current_tenant_id()
    async with get_session() as session:
        cartera = await get_owned(session, Cartera, rid) if rid else None
        if cartera is None:
            return RedirectResponse("/cobranza/carteras?flash=No encontrada&flash_type=danger", status_code=303)
        notas = (
            await session.execute(
                select(CarteraNota).where(CarteraNota.tenant_id == tid, CarteraNota.cartera_id == cartera.id, CarteraNota.deleted_at.is_(None)).order_by(CarteraNota.created_at.desc())
            )
        ).scalars().all()
        ctx.update(record_id=record_id, tab="notas", cartera=cartera.nombre, notas=[{"id": str(n.id), "nota": n.nota, "fecha": n.created_at.strftime("%Y-%m-%d %H:%M") if n.created_at else ""} for n in notas])
    return _render("cobranza/cartera_notas.html", ctx)


@router.post("/carteras/{record_id}/notas/guardar")
async def cartera_nota_save(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    nota = str(form.get("nota") or "").strip()
    if rid is not None and nota:
        async with get_session() as session:
            if await get_owned(session, Cartera, rid) is not None:
                session.add(CarteraNota(tenant_id=get_current_tenant_id(), cartera_id=rid, nota=nota, creado_por_rol="admin"))
    return RedirectResponse(f"/cobranza/carteras/{record_id}/notas?flash=Nota guardada&flash_type=success", status_code=303)


@router.post("/notas/{record_id}/eliminar")
async def nota_eliminar(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    destino = "/cobranza/carteras"
    if rid is not None:
        async with get_session() as session:
            nota = await get_owned(session, CarteraNota, rid)
            if nota is not None:
                nota.deleted_at = datetime.now(UTC)
                destino = f"/cobranza/carteras/{nota.cartera_id}/notas"
    return RedirectResponse(f"{destino}?flash=Nota eliminada&flash_type=success", status_code=303)


@router.get("/carteras/{record_id}/estadisticas", response_class=HTMLResponse)
async def cartera_estadisticas(request: Request, record_id: str) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    tid = get_current_tenant_id()
    desde_mes = datetime.now(UTC) - timedelta(days=30)
    async with get_session() as session:
        cartera = await get_owned(session, Cartera, rid) if rid else None
        if cartera is None:
            return RedirectResponse("/cobranza/carteras?flash=No encontrada&flash_type=danger", status_code=303)
        clientes = (await session.execute(select(func.count()).select_from(Cliente).where(Cliente.tenant_id == tid, Cliente.cartera_id == cartera.id, Cliente.deleted_at.is_(None)))).scalar_one()
        prestamos = (await session.execute(select(func.count()).select_from(Prestamo).where(Prestamo.tenant_id == tid, Prestamo.cartera_id == cartera.id, Prestamo.estatus == "vigente", Prestamo.deleted_at.is_(None)))).scalar_one()
        prestamo_ids = select(Prestamo.id).where(Prestamo.tenant_id == tid, Prestamo.cartera_id == cartera.id)
        saldo = (await session.execute(select(func.coalesce(func.sum(Cuota.valor_esperado), 0)).where(Cuota.tenant_id == tid, Cuota.prestamo_id.in_(prestamo_ids), Cuota.estatus.in_(("pendiente", "atrasada")), Cuota.deleted_at.is_(None)))).scalar_one()
        mora = (await session.execute(select(func.coalesce(func.sum(Cuota.valor_esperado), 0)).where(Cuota.tenant_id == tid, Cuota.prestamo_id.in_(prestamo_ids), Cuota.estatus == "atrasada", Cuota.deleted_at.is_(None)))).scalar_one()
        cobrado = (await session.execute(select(func.coalesce(func.sum(Pago.valor), 0)).where(Pago.tenant_id == tid, Pago.prestamo_id.in_(prestamo_ids), Pago.fecha_hora >= desde_mes, Pago.deleted_at.is_(None)))).scalar_one()
        gastos = (await session.execute(select(func.coalesce(func.sum(Gasto.valor), 0)).where(Gasto.tenant_id == tid, Gasto.cartera_id == cartera.id, Gasto.estado == "activo", Gasto.deleted_at.is_(None)))).scalar_one()

    def _fmt(v: Any) -> str:
        return f"{Decimal(v):,.0f}".replace(",", ".")

    ctx.update(
        record_id=record_id, tab="estadisticas", cartera=cartera.nombre,
        kpis=[
            {"label": "Clientes", "value": clientes},
            {"label": "Préstamos vigentes", "value": prestamos},
            {"label": "Saldo", "value": _fmt(saldo)},
            {"label": "Mora", "value": _fmt(mora)},
            {"label": "Cobrado (30 días)", "value": _fmt(cobrado)},
            {"label": "Gastos", "value": _fmt(gastos)},
        ],
    )
    return _render("cobranza/cartera_estadisticas.html", ctx)


async def _clientes_del_dia(session, cartera_id: uuid.UUID) -> list[dict[str, Any]]:
    """Clientes con préstamo vigente (trayecto: cobro de cuota / cuota en mora)."""
    tid = get_current_tenant_id()
    clientes = (await session.execute(select(Cliente).where(Cliente.tenant_id == tid, Cliente.cartera_id == cartera_id, Cliente.deleted_at.is_(None)))).scalars().all()
    out: list[dict[str, Any]] = []
    for c in clientes:
        prestamos = (await session.execute(select(Prestamo.id).where(Prestamo.tenant_id == tid, Prestamo.cliente_id == c.id, Prestamo.estatus == "vigente", Prestamo.deleted_at.is_(None)))).scalars().all()
        if not prestamos:
            continue
        cuotas = (await session.execute(select(Cuota).where(Cuota.tenant_id == tid, Cuota.prestamo_id.in_(prestamos), Cuota.estatus.in_(("pendiente", "atrasada")), Cuota.deleted_at.is_(None)))).scalars().all()
        saldo = sum((q.valor_esperado for q in cuotas), Decimal(0))
        mora = max(((date.today() - q.fecha_vencimiento).days for q in cuotas if q.fecha_vencimiento < date.today()), default=0)
        out.append({"id": str(c.id), "cliente": f"{c.nombre} {c.apellido or ''}".strip(), "telefono": c.telefono_whatsapp or "", "lat": c.lat, "lng": c.lng, "saldo": float(saldo), "mora": mora, "riesgo": c.color_riesgo})
    out.sort(key=lambda r: r["mora"], reverse=True)
    return out


@router.get("/carteras/{record_id}/trayecto", response_class=HTMLResponse)
async def cartera_trayecto(request: Request, record_id: str) -> HTMLResponse:
    ctx = await base_context(request, SECTION)
    if ctx is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=False):
        return RedirectResponse("/admin?flash=sin_pack&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    tid = get_current_tenant_id()
    async with get_session() as session:
        cartera = await get_owned(session, Cartera, rid) if rid else None
        if cartera is None:
            return RedirectResponse("/cobranza/carteras?flash=No encontrada&flash_type=danger", status_code=303)
        clientes = await _clientes_del_dia(session, cartera.id)
        aperturas = (await session.execute(select(CarteraApertura).where(CarteraApertura.tenant_id == tid, CarteraApertura.cartera_id == cartera.id).order_by(CarteraApertura.fecha.desc()).limit(10))).scalars().all()
        posiciones = (await session.execute(select(PosicionGestor).where(PosicionGestor.tenant_id == tid, PosicionGestor.cartera_id == cartera.id).order_by(PosicionGestor.registrada_en.desc()).limit(50))).scalars().all()
        ctx.update(
            record_id=record_id, tab="trayecto", cartera=cartera.nombre, clientes=clientes,
            puntos=[{"lat": c["lat"], "lng": c["lng"], "cliente": c["cliente"], "riesgo": c["riesgo"]} for c in clientes if c["lat"] is not None and c["lng"] is not None],
            aperturas=[{"fecha": a.fecha.isoformat(), "hora": a.hora_inicio or ""} for a in aperturas],
            posiciones=[{"lat": p.lat, "lng": p.lng, "fecha": p.registrada_en.strftime("%Y-%m-%d %H:%M") if p.registrada_en else ""} for p in posiciones],
        )
    return _render("cobranza/cartera_trayecto.html", ctx)


@router.post("/carteras/{record_id}/apertura")
async def cartera_apertura(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    if rid is not None:
        async with get_session() as session:
            if await get_owned(session, Cartera, rid) is not None:
                session.add(CarteraApertura(tenant_id=get_current_tenant_id(), cartera_id=rid, fecha=date.today(), hora_inicio=datetime.now(UTC).strftime("%H:%M")))
    return RedirectResponse(f"/cobranza/carteras/{record_id}/trayecto?flash=Apertura registrada&flash_type=success", status_code=303)


@router.post("/carteras/{record_id}/posicion")
async def cartera_posicion(request: Request, record_id: str) -> RedirectResponse:
    if await current_admin(request) is None:
        return RedirectResponse("/admin/login", status_code=303)
    if not await _authorized(request, write=True):
        return RedirectResponse("/admin?flash=sin_permiso&flash_type=danger", status_code=303)
    rid = _uuid(record_id)
    form = await request.form()
    try:
        lat = float(str(form.get("lat") or ""))
        lng = float(str(form.get("lng") or ""))
    except ValueError:
        return RedirectResponse(f"/cobranza/carteras/{record_id}/trayecto?flash=Coordenadas inválidas&flash_type=danger", status_code=303)
    if rid is not None:
        async with get_session() as session:
            if await get_owned(session, Cartera, rid) is not None:
                session.add(PosicionGestor(tenant_id=get_current_tenant_id(), cartera_id=rid, lat=lat, lng=lng))
    return RedirectResponse(f"/cobranza/carteras/{record_id}/trayecto?flash=Posición registrada&flash_type=success", status_code=303)


# --------------------------------------------------------------- helpers
async def _caja_de(session, cartera_id: uuid.UUID) -> Caja:
    tid = get_current_tenant_id()
    caja = (await session.execute(select(Caja).where(Caja.tenant_id == tid, Caja.cartera_id == cartera_id))).scalar_one_or_none()
    if caja is None:
        caja = Caja(tenant_id=tid, cartera_id=cartera_id, saldo_inicial=Decimal(0), saldo_actual=Decimal(0))
        session.add(caja)
        await session.flush()
    return caja


async def _generar_liquidacion(cartera_id: uuid.UUID, periodo: str) -> None:
    desde = {
        "diario": date.today(),
        "semanal": date.today() - timedelta(days=7),
        "quincenal": date.today() - timedelta(days=15),
        "mensual": date.today() - timedelta(days=30),
    }.get(periodo, date.today())
    tid = get_current_tenant_id()
    async with get_session() as session:
        cartera = await get_owned(session, Cartera, cartera_id)
        if cartera is None:
            return
        prestamo_ids = select(Prestamo.id).where(Prestamo.tenant_id == tid, Prestamo.cartera_id == cartera.id)
        cobrado = (await session.execute(select(func.coalesce(func.sum(Pago.valor), 0)).where(Pago.tenant_id == tid, Pago.prestamo_id.in_(prestamo_ids), Pago.fecha_hora >= datetime.combine(desde, datetime.min.time()), Pago.deleted_at.is_(None)))).scalar_one()
        gastos = (await session.execute(select(func.coalesce(func.sum(Gasto.valor), 0)).where(Gasto.tenant_id == tid, Gasto.cartera_id == cartera.id, Gasto.estado == "activo", Gasto.deleted_at.is_(None)))).scalar_one()
        session.add(Liquidacion(tenant_id=tid, cartera_id=cartera.id, fecha=date.today(), periodo=periodo, total_cobrado=cobrado, total_gastos=gastos))


# Evita imports sin uso si se recortan handlers.
# Modelos declarados (tabla + migración) pero aún sin UI en esta tanda.
_ = (CajaAjuste, CarteraEstadisticasSnapshot, CarteraOptimizadaLog, Promesa, ReporteDiario)
