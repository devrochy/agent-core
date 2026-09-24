"""Panel propio del pack de cobranza (`/cobranza/…`).

Navegación por secciones (ADR-0061). El **propietario es el tenant** (ADR-0062),
así que no hay CRUD de propietarios: se gestionan **Gestores, Carteras, Clientes**
y, dentro de cada cartera, Configuración / Clientes / Gastos / Inyecciones /
Liquidaciones.

Reutiliza el generador de formularios del core (`render_json_schema_form` /
`parse_json_schema_form`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from core.admin.auth import current_admin
from core.admin.context import base_context
from core.admin.form_generator import parse_json_schema_form, render_json_schema_form
from core.db.session import get_owned, get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_cobranza.models import (
    Caja,
    CajaAjuste,
    Cartera,
    CarteraConfig,
    Cliente,
    Cuota,
    Gasto,
    Gestor,
    GestorPermiso,
    Inyeccion,
    Liquidacion,
    Pago,
    Prestamo,
    Promesa,
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


async def _guard(request: Request, *, write: bool) -> bool:
    from core.admin.permissions import require_section

    return await require_section(request, SECTION, write=write)


async def _pack_enabled() -> bool:
    from core.agents.spec import enabled_packs, load_active_spec

    try:
        spec = await load_active_spec()
    except Exception:
        return False
    return PLUGIN_ID in enabled_packs(spec)


async def _authorized(request: Request, *, write: bool) -> bool:
    return await _guard(request, write=write) and await _pack_enabled()


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
            if gasto is not None:
                gasto.aprobado = True
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
                session.add(Inyeccion(tenant_id=get_current_tenant_id(), cartera_id=rid, valor=valor, comentario=str(form.get("comentario") or "").strip() or None))
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
_ = (CajaAjuste, Promesa)
