"""API admin del pack de Cobranza (ADR-0079).

Montada en `/api/v1/pack/cobranza`. Cubre lo más usado del panel: resumen,
reglas de negociación, carteras, cobros (pagos), promesas, gestores y las
secciones operativas (monitoreo, alertas de acceso y auditoría). El resto
(carteras a fondo, dispositivos, reportes, propietario, caja/liquidaciones)
sigue en el panel clásico enlazado.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import asin, cos, radians, sin, sqrt
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from core.admin.auth import hash_password
from core.api.deps import Principal, get_principal, require
from core.api.errors import ApiError
from core.db.models import Admin, Conversation, Escalation, TenantUser
from core.db.session import get_owned, get_session
from pack_cobranza.permisos import GESTOR_PERMISOS

# Registra los providers de búsqueda global del pack (ADR-0096).
from . import search as _search  # noqa: F401
from . import sync as _sync  # noqa: F401
from .models import (
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
    ConversacionPropietario,
    Cuota,
    Device,
    Gasto,
    Gestor,
    GestorPermiso,
    IntentoAcceso,
    Inyeccion,
    LinkPago,
    Liquidacion,
    MensajePropietario,
    Pago,
    PosicionGestor,
    Prestamo,
    Promesa,
    ReglaNegociacion,
    ReporteDiario,
    SyncEvento,
    Visita,
)

PLUGIN_ID = "pack_cobranza"
router = APIRouter(prefix="/api/v1/pack/cobranza", tags=["pack · cobranza"])

PROMISE_STATUSES = ("pendiente", "cumplida", "incumplida")


async def _get_active(
    session: Any, model: type[Any], id_: uuid.UUID, tid: uuid.UUID
) -> Any | None:
    """Como `get_owned` pero excluye filas con soft delete (ADR-0001)."""
    obj = await get_owned(session, model, id_, tid)
    if obj is not None and getattr(obj, "deleted_at", None) is not None:
        return None
    return obj


# ------------------------------------------------------------------ resumen
class Kpi(BaseModel):
    label: str
    value: str


class Summary(BaseModel):
    kpis: list[Kpi]


@router.get("/summary", response_model=Summary)
async def cobranza_summary(
    principal: Annotated[Principal, Depends(require("pack_cobranza:resumen"))],
) -> Summary:
    tid = principal.tenant_id
    semana = datetime.now(UTC) - timedelta(days=7)
    mes = datetime.now(UTC) - timedelta(days=30)
    async with get_session() as session:
        carteras = (
            await session.execute(
                select(func.count())
                .select_from(Cartera)
                .where(Cartera.tenant_id == tid, Cartera.deleted_at.is_(None))
            )
        ).scalar_one()
        clientes = (
            await session.execute(
                select(func.count())
                .select_from(Cliente)
                .where(Cliente.tenant_id == tid, Cliente.deleted_at.is_(None))
            )
        ).scalar_one()
        prestamos = (
            await session.execute(
                select(func.count())
                .select_from(Prestamo)
                .where(
                    Prestamo.tenant_id == tid,
                    Prestamo.estatus == "vigente",
                    Prestamo.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        saldo = (
            await session.execute(
                select(func.coalesce(func.sum(Cuota.valor_esperado), 0)).where(
                    Cuota.tenant_id == tid,
                    Cuota.estatus.in_(("pendiente", "atrasada")),
                    Cuota.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        mora = (
            await session.execute(
                select(func.coalesce(func.sum(Cuota.valor_esperado), 0)).where(
                    Cuota.tenant_id == tid,
                    Cuota.estatus == "atrasada",
                    Cuota.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        cobrado = (
            await session.execute(
                select(func.coalesce(func.sum(Pago.valor), 0)).where(
                    Pago.tenant_id == tid,
                    Pago.fecha_hora >= semana,
                    Pago.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        gastos = (
            await session.execute(
                select(func.coalesce(func.sum(Gasto.valor), 0)).where(
                    Gasto.tenant_id == tid,
                    Gasto.fecha_hora >= mes,
                    Gasto.estado == "activo",
                    Gasto.deleted_at.is_(None),
                )
            )
        ).scalar_one()

    def money(value: Any) -> str:
        return f"{Decimal(value):,.0f}".replace(",", ".")

    return Summary(
        kpis=[
            Kpi(label="Carteras", value=str(carteras)),
            Kpi(label="Clientes", value=str(clientes)),
            Kpi(label="Préstamos vigentes", value=str(prestamos)),
            Kpi(label="Saldo en cartera", value=money(saldo)),
            Kpi(label="Mora", value=money(mora)),
            Kpi(label="Cobrado (7 días)", value=money(cobrado)),
            Kpi(label="Gastos (30 días)", value=money(gastos)),
        ]
    )


# ------------------------------------------------------------------ reglas
AlcancePromesa = Literal["ambos", "vencido", "saldo_total"]


class Rules(BaseModel):
    max_dias_prorroga: int
    min_abono_pct: float
    max_reprogramaciones: int
    umbral_saldo_autonomo: float
    alcance_promesa: AlcancePromesa = Field(
        default="ambos",
        description="Alcances de la promesa de pago que acepta el asistente: lo vencido, el saldo total o ambos (HU-AOS-08).",
    )


class RulesUpdate(BaseModel):
    # Rangos explícitos: un negativo responde 422 con el campo marcado (antes se recortaba a 0
    # y el panel decía «guardado»). P0 de pruebas 2026-10-17.
    max_dias_prorroga: int = Field(default=15, ge=0, le=365)
    min_abono_pct: float = Field(default=10.0, ge=0, le=100)
    max_reprogramaciones: int = Field(default=2, ge=0, le=100)
    umbral_saldo_autonomo: float = Field(default=0.0, ge=0)
    alcance_promesa: AlcancePromesa | None = Field(
        default=None,
        description="Alcances de la promesa de pago (ambos, vencido o saldo_total). Si no se envía, se conserva el actual.",
    )


@router.get("/rules", response_model=Rules)
async def get_rules(
    principal: Annotated[Principal, Depends(require("pack_cobranza:reglas"))],
) -> Rules:
    async with get_session() as session:
        row = (
            await session.execute(
                select(ReglaNegociacion).where(ReglaNegociacion.tenant_id == principal.tenant_id)
            )
        ).scalar_one_or_none()
    return Rules(
        max_dias_prorroga=row.max_dias_prorroga if row else 15,
        min_abono_pct=float(row.min_abono_pct) if row else 10.0,
        max_reprogramaciones=row.max_reprogramaciones if row else 2,
        umbral_saldo_autonomo=float(row.umbral_saldo_autonomo) if row else 0.0,
        alcance_promesa=_alcance_de_reglas(row),
    )


def _alcance_de_reglas(row: ReglaNegociacion | None) -> AlcancePromesa:
    valor = row.alcance_promesa if row is not None else None
    if valor == "vencido" or valor == "saldo_total":
        return valor
    return "ambos"


@router.put("/rules", response_model=Rules)
async def save_rules(
    payload: RulesUpdate,
    principal: Annotated[Principal, Depends(require("pack_cobranza:reglas", write=True))],
) -> Rules:
    tid = principal.tenant_id
    async with get_session() as session:
        row = (
            await session.execute(
                select(ReglaNegociacion).where(ReglaNegociacion.tenant_id == tid)
            )
        ).scalar_one_or_none()
        if row is None:
            row = ReglaNegociacion(tenant_id=tid)
            session.add(row)
        row.max_dias_prorroga = max(0, payload.max_dias_prorroga)
        row.min_abono_pct = Decimal(str(max(0.0, payload.min_abono_pct)))
        row.max_reprogramaciones = max(0, payload.max_reprogramaciones)
        row.umbral_saldo_autonomo = Decimal(str(max(0.0, payload.umbral_saldo_autonomo)))
        if payload.alcance_promesa is not None:
            row.alcance_promesa = payload.alcance_promesa
    return await get_rules(principal)


# ------------------------------------------------------------------ carteras
class Portfolio(BaseModel):
    id: uuid.UUID
    nombre: str
    descripcion: str | None
    gestor: str | None
    gestor_id: uuid.UUID | None = None
    tipo_interes: float
    num_cuotas: int
    moneda: str
    estatus: str
    costo_cobro: float = 0.0


@router.get("/portfolios", response_model=list[Portfolio])
async def list_portfolios(
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras"))],
) -> list[Portfolio]:
    tid = principal.tenant_id
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(Cartera)
                    .where(Cartera.tenant_id == tid, Cartera.deleted_at.is_(None))
                    .order_by(Cartera.nombre)
                )
            )
            .scalars()
            .all()
        )
        gestores = (
            (
                await session.execute(
                    select(Gestor).where(Gestor.tenant_id == tid, Gestor.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
    name_by_id = {g.id: f"{g.nombre} {g.apellido or ''}".strip() for g in gestores}
    return [
        _as_portfolio(c, name_by_id.get(c.gestor_id) if c.gestor_id else None) for c in rows
    ]


# ------------------------------------------------------------------ cobros
class Payment(BaseModel):
    id: uuid.UUID
    cliente: str
    valor: float
    metodo_pago: str
    registrado_por: str | None
    fecha_hora: datetime


@router.get("/payments", response_model=list[Payment])
async def list_payments(
    principal: Annotated[Principal, Depends(require("pack_cobranza:cobros"))],
) -> list[Payment]:
    tid = principal.tenant_id
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(Pago)
                    .where(Pago.tenant_id == tid, Pago.deleted_at.is_(None))
                    .order_by(Pago.fecha_hora.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        clientes = (
            (
                await session.execute(
                    select(Cliente).where(Cliente.tenant_id == tid, Cliente.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
    name_by_id = {c.id: f"{c.nombre} {c.apellido or ''}".strip() for c in clientes}
    return [
        Payment(
            id=p.id,
            cliente=name_by_id.get(p.cliente_id, "Cliente"),
            valor=float(p.valor or 0),
            metodo_pago=p.metodo_pago or "efectivo",
            registrado_por=p.registrado_por,
            fecha_hora=p.fecha_hora,
        )
        for p in rows
    ]


# ------------------------------------------------------------------ promesas
class Promise(BaseModel):
    id: uuid.UUID
    cliente: str
    fecha_prometida: date
    valor_prometido: float
    estado: str
    tipo: str
    creado_por: str
    alcance: str | None = Field(
        default=None,
        description="Por qué monto fue la promesa del asistente: vencido o saldo_total (null en las demás).",
    )


class PromiseStatusUpdate(BaseModel):
    estado: str


@router.get("/promises", response_model=list[Promise])
async def list_promises(
    principal: Annotated[Principal, Depends(require("pack_cobranza:promesas"))],
) -> list[Promise]:
    tid = principal.tenant_id
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(Promesa)
                    .where(Promesa.tenant_id == tid, Promesa.deleted_at.is_(None))
                    .order_by(Promesa.fecha_prometida.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        clientes = (
            (
                await session.execute(
                    select(Cliente).where(Cliente.tenant_id == tid, Cliente.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
    name_by_id = {c.id: f"{c.nombre} {c.apellido or ''}".strip() for c in clientes}
    return [
        Promise(
            id=p.id,
            cliente=name_by_id.get(p.cliente_id, "Cliente"),
            fecha_prometida=p.fecha_prometida,
            valor_prometido=float(p.valor_prometido or 0),
            estado=p.estado,
            tipo=p.tipo,
            creado_por=p.creado_por,
            alcance=p.alcance,
        )
        for p in rows
    ]


@router.post("/promises/{promise_id}/status", status_code=204)
async def update_promise_status(
    promise_id: uuid.UUID,
    payload: PromiseStatusUpdate,
    principal: Annotated[Principal, Depends(require("pack_cobranza:promesas", write=True))],
) -> Response:
    if payload.estado not in PROMISE_STATUSES:
        raise ApiError.bad_request(
            f"Estado inválido: {payload.estado}. Válidos: {', '.join(PROMISE_STATUSES)}",
            "invalid_status",
        )
    async with get_session() as session:
        row = await _get_active(session, Promesa, promise_id, principal.tenant_id)
        if row is None:
            raise ApiError.not_found("Promesa no encontrada.")
        row.estado = payload.estado
    return Response(status_code=204)


# ------------------------------------------------------------------ gestores
class Collector(BaseModel):
    id: uuid.UUID
    nombre: str
    correo: str | None
    telefono: str | None
    codigo: str | None
    estatus: str


@router.get("/collectors", response_model=list[Collector])
async def list_collectors(
    principal: Annotated[Principal, Depends(require("pack_cobranza:gestores"))],
) -> list[Collector]:
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(Gestor)
                    .where(
                        Gestor.tenant_id == principal.tenant_id,
                        Gestor.deleted_at.is_(None),
                    )
                    .order_by(Gestor.nombre)
                )
            )
            .scalars()
            .all()
        )
    return [
        Collector(
            id=g.id,
            nombre=f"{g.nombre} {g.apellido or ''}".strip(),
            correo=g.correo,
            telefono=g.telefono,
            codigo=g.codigo,
            estatus=g.estatus,
        )
        for g in rows
    ]


# ------------------------------------------------------------------ monitoreo
class Monitoring(BaseModel):
    conversaciones: int
    escalaciones: int
    promesas_pendientes: int
    promesas_cumplidas: int
    promesas_incumplidas: int
    ultimas_promesas: list[Promise]


def _name_by_id(rows: list[Cliente]) -> dict[uuid.UUID, str]:
    return {c.id: f"{c.nombre} {c.apellido or ''}".strip() for c in rows}


def _as_promise(row: Promesa, cliente: str) -> Promise:
    return Promise(
        id=row.id,
        cliente=cliente,
        fecha_prometida=row.fecha_prometida,
        valor_prometido=float(row.valor_prometido or 0),
        estado=row.estado,
        tipo=row.tipo,
        creado_por=row.creado_por,
        alcance=row.alcance,
    )


@router.get("/monitoring", response_model=Monitoring)
async def monitoring(
    principal: Annotated[Principal, Depends(require("pack_cobranza:monitoreo"))],
) -> Monitoring:
    tid = principal.tenant_id
    async with get_session() as session:
        conversaciones = (
            await session.execute(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.tenant_id == tid)
            )
        ).scalar_one()
        escalaciones = (
            await session.execute(
                select(func.count()).select_from(Escalation).where(Escalation.tenant_id == tid)
            )
        ).scalar_one()
        rows = (
            (
                await session.execute(
                    select(Promesa)
                    .where(Promesa.tenant_id == tid, Promesa.deleted_at.is_(None))
                    .order_by(Promesa.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        clientes = (
            (
                await session.execute(
                    select(Cliente).where(Cliente.tenant_id == tid, Cliente.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
    nombres = _name_by_id(clientes)
    return Monitoring(
        conversaciones=conversaciones,
        escalaciones=escalaciones,
        promesas_pendientes=sum(1 for p in rows if p.estado == "pendiente"),
        promesas_cumplidas=sum(1 for p in rows if p.estado == "cumplida"),
        promesas_incumplidas=sum(1 for p in rows if p.estado == "incumplida"),
        ultimas_promesas=[_as_promise(p, nombres.get(p.cliente_id, "—")) for p in rows],
    )


# ------------------------------------------------------------------ alertas
class AccessAttempt(BaseModel):
    fecha: str
    gestor: str
    resultado: str
    motivo: str
    imei: str
    critico: bool


class Alerts(BaseModel):
    total: int
    denegados: int
    items: list[AccessAttempt]


@router.get("/alerts", response_model=Alerts)
async def list_alerts(
    principal: Annotated[Principal, Depends(require("pack_cobranza:alertas"))],
) -> Alerts:
    tid = principal.tenant_id
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(IntentoAcceso)
                    .where(IntentoAcceso.tenant_id == tid)
                    .order_by(IntentoAcceso.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        gestores = (
            (
                await session.execute(
                    select(Gestor).where(Gestor.tenant_id == tid)
                )
            )
            .scalars()
            .all()
        )
    nombres = {g.id: f"{g.nombre} {g.apellido or ''}".strip() for g in gestores}
    items = [
        AccessAttempt(
            fecha=r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
            gestor=nombres.get(r.gestor_id, "—") if r.gestor_id else "—",
            resultado=r.resultado,
            motivo=r.motivo or "",
            imei=r.imei or "",
            critico=r.resultado != "ok",
        )
        for r in rows
    ]
    return Alerts(
        total=len(items),
        denegados=sum(1 for i in items if i.critico),
        items=items,
    )


# ------------------------------------------------------------------ auditoría
class AuditRow(BaseModel):
    fecha: str
    entidad: str
    entidad_id: str
    operacion: str
    antes: dict[str, Any] | None
    despues: dict[str, Any] | None
    actor: str
    motivo: str


class AuditLog(BaseModel):
    entidades: list[str]
    operaciones: list[str]
    filas: list[AuditRow]


@router.get("/audit", response_model=AuditLog)
async def audit_log(
    principal: Annotated[Principal, Depends(require("pack_cobranza:auditoria"))],
    entidad: str = "",
    operacion: str = "",
    desde: str = "",
    hasta: str = "",
) -> AuditLog:
    tid = principal.tenant_id
    query = select(AuditoriaCartera).where(AuditoriaCartera.tenant_id == tid)
    if entidad:
        query = query.where(AuditoriaCartera.entidad == entidad)
    if operacion:
        query = query.where(AuditoriaCartera.operacion == operacion)
    for valor, es_desde in ((desde, True), (hasta, False)):
        if not valor:
            continue
        try:
            dia = date.fromisoformat(valor)
        except ValueError:
            continue
        limite = datetime.combine(
            dia if es_desde else dia + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        )
        query = (
            query.where(AuditoriaCartera.created_at >= limite)
            if es_desde
            else query.where(AuditoriaCartera.created_at < limite)
        )
    async with get_session() as session:
        filas = (
            (await session.execute(query.order_by(AuditoriaCartera.created_at.desc()).limit(200)))
            .scalars()
            .all()
        )
        entidades = sorted(
            e
            for (e,) in (
                await session.execute(
                    select(AuditoriaCartera.entidad)
                    .where(AuditoriaCartera.tenant_id == tid)
                    .distinct()
                )
            ).all()
            if e
        )
        operaciones = sorted(
            o
            for (o,) in (
                await session.execute(
                    select(AuditoriaCartera.operacion)
                    .where(AuditoriaCartera.tenant_id == tid)
                    .distinct()
                )
            ).all()
            if o
        )
    return AuditLog(
        entidades=entidades,
        operaciones=operaciones,
        filas=[
            AuditRow(
                fecha=r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
                entidad=r.entidad,
                entidad_id=str(r.entidad_id)[:8],
                operacion=r.operacion,
                antes=r.valores_antes,
                despues=r.valores_despues,
                actor=r.actor_rol or "—",
                motivo=r.motivo or "",
            )
            for r in filas
        ],
    )


# ---------------------------------------------------------------- dispositivos
class DeviceRow(BaseModel):
    id: uuid.UUID
    codigo: str
    gestor: str
    imei: str
    whatsapp: str
    estado: str
    vinculado: str


class AttemptRow(BaseModel):
    resultado: str
    motivo: str
    imei: str
    fecha: str


class SyncRow(BaseModel):
    fecha: str
    tipo: str
    estado: str
    gestor: str
    evento: str
    critico: bool


class Devices(BaseModel):
    pendientes: int
    eventos_error: int
    dispositivos: list[DeviceRow]
    intentos: list[AttemptRow]
    eventos: list[SyncRow]


def _audit(
    session: Any,
    tid: uuid.UUID,
    entidad: str,
    entidad_id: uuid.UUID,
    operacion: str,
    *,
    antes: dict[str, Any] | None = None,
    despues: dict[str, Any] | None = None,
    motivo: str | None = None,
) -> None:
    session.add(
        AuditoriaCartera(
            tenant_id=tid,
            entidad=entidad,
            entidad_id=entidad_id,
            operacion=operacion,
            valores_antes=antes,
            valores_despues=despues,
            motivo=motivo,
            actor_rol="admin",
        )
    )


@router.get("/devices", response_model=Devices)
async def list_devices(
    principal: Annotated[Principal, Depends(require("pack_cobranza:dispositivos"))],
) -> Devices:
    tid = principal.tenant_id
    async with get_session() as session:
        devices = (
            (
                await session.execute(
                    select(Device)
                    .where(Device.tenant_id == tid, Device.deleted_at.is_(None))
                    .order_by(Device.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        gestores = (
            (await session.execute(select(Gestor).where(Gestor.tenant_id == tid))).scalars().all()
        )
        intentos = (
            (
                await session.execute(
                    select(IntentoAcceso)
                    .where(IntentoAcceso.tenant_id == tid)
                    .order_by(IntentoAcceso.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        eventos = (
            (
                await session.execute(
                    select(SyncEvento)
                    .where(SyncEvento.tenant_id == tid)
                    .order_by(SyncEvento.created_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
    nombres = {g.id: f"{g.nombre} {g.apellido or ''}".strip() for g in gestores}
    por_device = {d.id: nombres.get(d.gestor_id, "—") if d.gestor_id else "—" for d in devices}
    return Devices(
        pendientes=sum(1 for d in devices if d.estado == "pendiente"),
        eventos_error=sum(1 for e in eventos if e.estado != "aplicado"),
        dispositivos=[
            DeviceRow(
                id=d.id,
                codigo=d.codigo,
                gestor=nombres.get(d.gestor_id, "—") if d.gestor_id else "—",
                imei=d.imei or "",
                whatsapp=d.whatsapp_number or "",
                estado=d.estado,
                vinculado=d.fecha_vinculacion.strftime("%Y-%m-%d %H:%M") if d.fecha_vinculacion else "",
            )
            for d in devices
        ],
        intentos=[
            AttemptRow(
                resultado=i.resultado,
                motivo=i.motivo or "",
                imei=i.imei or "",
                fecha=i.created_at.strftime("%Y-%m-%d %H:%M") if i.created_at else "",
            )
            for i in intentos
        ],
        eventos=[
            SyncRow(
                fecha=e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else "",
                tipo=e.tipo,
                estado=e.estado,
                gestor=por_device.get(e.device_id, "—") if e.device_id else "—",
                evento=(e.evento_id_cliente or "")[:8],
                critico=e.estado != "aplicado",
            )
            for e in eventos
        ],
    )


class DeviceStatusUpdate(BaseModel):
    estado: str


DEVICE_STATUSES = ("activo", "pendiente", "bloqueado")


@router.post("/devices/{device_id}/status", status_code=204)
async def set_device_status(
    device_id: uuid.UUID,
    payload: DeviceStatusUpdate,
    principal: Annotated[Principal, Depends(require("pack_cobranza:dispositivos", write=True))],
) -> Response:
    if payload.estado not in DEVICE_STATUSES:
        raise ApiError.bad_request(
            f"Estado inválido: {payload.estado}. Válidos: {', '.join(DEVICE_STATUSES)}",
            "invalid_status",
        )
    async with get_session() as session:
        device = await _get_active(session, Device, device_id, principal.tenant_id)
        if device is None:
            raise ApiError.not_found("Dispositivo no encontrado.")
        device.estado = payload.estado
        if payload.estado == "activo" and device.fecha_vinculacion is None:
            device.fecha_vinculacion = datetime.now(UTC)
        _audit(
            session,
            principal.tenant_id,
            "dispositivo",
            device.id,
            "editar",
            despues={"estado": payload.estado},
            motivo="Aprobación de dispositivo",
        )
    return Response(status_code=204)


# --------------------------------------------------------- cobros al propietario
class OwnerCharge(BaseModel):
    id: uuid.UUID
    periodo: str
    monto: float
    pagado: float | None
    vence: date
    estado: str


class PaymentLink(BaseModel):
    referencia: str
    url: str
    monto: float
    estado: str


class OwnerCharges(BaseModel):
    cobros: list[OwnerCharge]
    links: list[PaymentLink]


def _as_charge(row: CobroPropietario) -> OwnerCharge:
    return OwnerCharge(
        id=row.id,
        periodo=row.periodo,
        monto=float(row.monto_calculado or 0),
        pagado=float(row.monto_pagado) if row.monto_pagado is not None else None,
        vence=row.fecha_vencimiento,
        estado=row.estado,
    )


@router.get("/owner-charges", response_model=OwnerCharges)
async def list_owner_charges(
    principal: Annotated[Principal, Depends(require("pack_cobranza:cobros"))],
) -> OwnerCharges:
    tid = principal.tenant_id
    async with get_session() as session:
        cobros = (
            (
                await session.execute(
                    select(CobroPropietario)
                    .where(
                        CobroPropietario.tenant_id == tid,
                        CobroPropietario.deleted_at.is_(None),
                    )
                    .order_by(CobroPropietario.periodo.desc())
                )
            )
            .scalars()
            .all()
        )
        links = (
            (
                await session.execute(
                    select(LinkPago)
                    .where(LinkPago.tenant_id == tid)
                    .order_by(LinkPago.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
    return OwnerCharges(
        cobros=[_as_charge(c) for c in cobros],
        links=[
            PaymentLink(
                referencia=lk.referencia,
                url=lk.url,
                monto=float(lk.monto or 0),
                estado=lk.estado,
            )
            for lk in links
        ],
    )


class OwnerChargeCreate(BaseModel):
    periodo: str
    monto: float
    fecha_vencimiento: date


@router.post("/owner-charges", response_model=OwnerCharge, status_code=201)
async def create_owner_charge(
    payload: OwnerChargeCreate,
    principal: Annotated[Principal, Depends(require("pack_cobranza:cobros", write=True))],
) -> OwnerCharge:
    from .admin import registrar_mensaje_propietario

    periodo = payload.periodo.strip()
    if not periodo:
        raise ApiError.bad_request("Falta el periodo.", "missing_period")
    if payload.monto <= 0:
        raise ApiError.bad_request("El monto debe ser mayor a cero.", "invalid_amount")
    tid = principal.tenant_id
    async with get_session() as session:
        cobro = (
            await session.execute(
                select(CobroPropietario).where(
                    CobroPropietario.tenant_id == tid,
                    CobroPropietario.periodo == periodo,
                )
            )
        ).scalar_one_or_none()
        if cobro is None:
            cobro = CobroPropietario(
                tenant_id=tid,
                periodo=periodo,
                monto_calculado=Decimal(str(payload.monto)),
                fecha_vencimiento=payload.fecha_vencimiento,
            )
            session.add(cobro)
            await session.flush()
        else:
            cobro.monto_calculado = Decimal(str(payload.monto))
            cobro.fecha_vencimiento = payload.fecha_vencimiento
        await registrar_mensaje_propietario(
            session,
            tid,
            f"Notificación de cobro · periodo {periodo}: {payload.monto:,.0f} · "
            f"vence {payload.fecha_vencimiento.isoformat()}",
            tipo="notificacion",
            autor_rol="sistema",
            cobro_id=cobro.id,
        )
        await session.refresh(cobro)
        return _as_charge(cobro)


class OwnerPayment(BaseModel):
    monto_pagado: float
    metodo_pago: str | None = None


@router.post("/owner-charges/{charge_id}/payment", status_code=204)
async def register_owner_payment(
    charge_id: uuid.UUID,
    payload: OwnerPayment,
    principal: Annotated[Principal, Depends(require("pack_cobranza:cobros", write=True))],
) -> Response:
    if payload.monto_pagado <= 0:
        raise ApiError.bad_request("El monto debe ser mayor a cero.", "invalid_amount")
    async with get_session() as session:
        cobro = await _get_active(session, CobroPropietario, charge_id, principal.tenant_id)
        if cobro is None:
            raise ApiError.not_found("Cobro no encontrado.")
        cobro.monto_pagado = Decimal(str(payload.monto_pagado))
        cobro.fecha_pago = date.today()
        cobro.estado = "pagado"
        if payload.metodo_pago:
            cobro.metodo_pago = payload.metodo_pago
        cobro.registrado_por = principal.admin.email
    return Response(status_code=204)


# ------------------------------------------------------------------ reportes
class ReportRow(BaseModel):
    cartera: str
    clientes: int
    prestamos: int
    saldo: float
    vencidas: int
    cobrado: float
    caja: float
    gastos: float


class Liquidation(BaseModel):
    cartera: str
    fecha: date
    periodo: str
    cobrado: float
    gastos: float
    comision: float


class Reports(BaseModel):
    filas: list[ReportRow]
    liquidaciones: list[Liquidation]


@router.get("/reports", response_model=Reports)
async def reports(
    principal: Annotated[Principal, Depends(require("pack_cobranza:reportes"))],
) -> Reports:
    tid = principal.tenant_id
    async with get_session() as session:
        carteras = (
            (
                await session.execute(
                    select(Cartera)
                    .where(Cartera.tenant_id == tid, Cartera.deleted_at.is_(None))
                    .order_by(Cartera.nombre)
                )
            )
            .scalars()
            .all()
        )
        filas: list[ReportRow] = []
        for c in carteras:
            n_clientes = (
                await session.execute(
                    select(func.count())
                    .select_from(Cliente)
                    .where(
                        Cliente.tenant_id == tid,
                        Cliente.cartera_id == c.id,
                        Cliente.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            prestamos = (
                (
                    await session.execute(
                        select(Prestamo).where(
                            Prestamo.tenant_id == tid,
                            Prestamo.cartera_id == c.id,
                            Prestamo.deleted_at.is_(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            saldo = sum((p.saldo for p in prestamos), Decimal(0))
            vencidas = (
                await session.execute(
                    select(func.count())
                    .select_from(Cuota)
                    .join(Prestamo, Cuota.prestamo_id == Prestamo.id)
                    .where(
                        Cuota.tenant_id == tid,
                        Prestamo.cartera_id == c.id,
                        Cuota.estatus != "pagada",
                        Cuota.fecha_vencimiento < date.today(),
                        Cuota.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            cobrado = (
                await session.execute(
                    select(func.coalesce(func.sum(Pago.valor), 0)).where(
                        Pago.tenant_id == tid,
                        Pago.cliente_id.in_([p.cliente_id for p in prestamos])
                        if prestamos
                        else False,
                        Pago.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            gastos = (
                await session.execute(
                    select(func.coalesce(func.sum(Gasto.valor), 0)).where(
                        Gasto.tenant_id == tid,
                        Gasto.cartera_id == c.id,
                        Gasto.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            caja = (
                await session.execute(
                    select(Caja).where(Caja.tenant_id == tid, Caja.cartera_id == c.id)
                )
            ).scalar_one_or_none()
            filas.append(
                ReportRow(
                    cartera=c.nombre,
                    clientes=int(n_clientes),
                    prestamos=len(prestamos),
                    saldo=float(saldo),
                    vencidas=int(vencidas),
                    cobrado=float(cobrado),
                    caja=float(caja.saldo_actual) if caja is not None else 0.0,
                    gastos=float(gastos),
                )
            )
        liquidaciones = (
            (
                await session.execute(
                    select(Liquidacion)
                    .where(Liquidacion.tenant_id == tid, Liquidacion.deleted_at.is_(None))
                    .order_by(Liquidacion.fecha.desc())
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
    nombres = {c.id: c.nombre for c in carteras}
    return Reports(
        filas=filas,
        liquidaciones=[
            Liquidation(
                cartera=nombres.get(liq.cartera_id, "—"),
                fecha=liq.fecha,
                periodo=liq.periodo,
                cobrado=float(liq.total_cobrado or 0),
                gastos=float(liq.total_gastos or 0),
                comision=float(liq.comision_valor or 0),
            )
            for liq in liquidaciones
        ],
    )


# ------------------------------------------------- hilo con el propietario
class OwnerMessage(BaseModel):
    id: uuid.UUID
    direccion: str
    tipo: str
    autor: str
    contenido: str
    fecha: str


class OwnerThread(BaseModel):
    whatsapp: str
    asunto: str
    estado: str
    abierto: bool
    mensajes: list[OwnerMessage]


@router.get("/owner-thread", response_model=OwnerThread)
async def owner_thread(
    principal: Annotated[Principal, Depends(require("pack_cobranza:propietario"))],
) -> OwnerThread:
    from .admin import _propietario_whatsapp

    tid = principal.tenant_id
    async with get_session() as session:
        conv = (
            await session.execute(
                select(ConversacionPropietario).where(
                    ConversacionPropietario.tenant_id == tid,
                    ConversacionPropietario.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        mensajes = (
            []
            if conv is None
            else (
                (
                    await session.execute(
                        select(MensajePropietario)
                        .where(
                            MensajePropietario.tenant_id == tid,
                            MensajePropietario.conversacion_id == conv.id,
                            MensajePropietario.deleted_at.is_(None),
                        )
                        .order_by(MensajePropietario.created_at)
                    )
                )
                .scalars()
                .all()
            )
        )
    return OwnerThread(
        whatsapp=await _propietario_whatsapp(),
        asunto=conv.asunto if conv else "Comunicación con el propietario",
        estado=conv.estado if conv else "abierta",
        abierto=conv is not None,
        mensajes=[
            OwnerMessage(
                id=m.id,
                direccion=m.direccion,
                tipo=m.tipo,
                autor=m.autor_rol or "",
                contenido=m.contenido,
                fecha=m.created_at.strftime("%Y-%m-%d %H:%M") if m.created_at else "",
            )
            for m in mensajes
        ],
    )


class OwnerMessageCreate(BaseModel):
    contenido: str
    direccion: str = "saliente"


@router.post("/owner-thread/messages", status_code=204)
async def create_owner_message(
    payload: OwnerMessageCreate,
    principal: Annotated[Principal, Depends(require("pack_cobranza:propietario", write=True))],
) -> Response:
    from .admin import registrar_mensaje_propietario

    contenido = payload.contenido.strip()
    if not contenido:
        raise ApiError.bad_request("Escribe un mensaje.", "missing_content")
    direccion = payload.direccion if payload.direccion in ("saliente", "entrante") else "saliente"
    async with get_session() as session:
        mensaje = await registrar_mensaje_propietario(
            session,
            principal.tenant_id,
            contenido,
            direccion=direccion,
            autor_rol="admin" if direccion == "saliente" else "propietario",
        )
        _audit(
            session,
            principal.tenant_id,
            "mensaje_propietario",
            mensaje.id,
            "crear",
            despues={"direccion": direccion},
        )
    return Response(status_code=204)


# ------------------------------------------------------- detalle de cartera
class PortfolioCaja(BaseModel):
    saldo_inicial: float
    saldo_actual: float
    actualizado: str


class Injection(BaseModel):
    fecha: str
    valor: float
    comentario: str
    estado: str


class Expense(BaseModel):
    id: uuid.UUID
    fecha: str
    descripcion: str
    valor: float
    aprobado: bool


class EstadisticaSnapshot(BaseModel):
    fecha: date
    datos: dict[str, Any]


class ReporteDiarioOut(BaseModel):
    fecha: str
    cobrado: float
    prestado: float
    visitados: int
    sin_pago: int
    inicio: str
    fin: str


class PortfolioDetail(BaseModel):
    id: uuid.UUID
    nombre: str
    descripcion: str | None
    gestor: str | None
    tipo_interes: float
    num_cuotas: int
    moneda: str
    estatus: str
    clientes: int
    prestamos: int
    saldo: float
    # KPIs del clásico (cartera_estadisticas): mora, cobrado de los últimos 30
    # días y gastos activos totales.
    mora: float = 0.0
    cobrado_30d: float = 0.0
    gastos_total: float = 0.0
    reportes_diarios: list[ReporteDiarioOut] = []
    caja: PortfolioCaja
    liquidaciones: list[Liquidation]
    inyecciones: list[Injection]
    gastos: list[Expense]
    estadisticas: list[EstadisticaSnapshot] = []


@router.get("/portfolios/{cartera_id}", response_model=PortfolioDetail)
async def portfolio_detail(
    cartera_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras"))],
) -> PortfolioDetail:
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        gestor = (
            await session.execute(
                select(Gestor).where(Gestor.tenant_id == tid, Gestor.id == cartera.gestor_id)
            )
        ).scalar_one_or_none() if cartera.gestor_id else None
        clientes = (
            await session.execute(
                select(func.count())
                .select_from(Cliente)
                .where(
                    Cliente.tenant_id == tid,
                    Cliente.cartera_id == cartera.id,
                    Cliente.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        prestamos = (
            (
                await session.execute(
                    select(Prestamo).where(
                        Prestamo.tenant_id == tid,
                        Prestamo.cartera_id == cartera.id,
                        Prestamo.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        caja = (
            await session.execute(
                select(Caja).where(Caja.tenant_id == tid, Caja.cartera_id == cartera.id)
            )
        ).scalar_one_or_none()
        liquidaciones = (
            (
                await session.execute(
                    select(Liquidacion)
                    .where(
                        Liquidacion.tenant_id == tid,
                        Liquidacion.cartera_id == cartera.id,
                        Liquidacion.deleted_at.is_(None),
                    )
                    .order_by(Liquidacion.fecha.desc())
                    .limit(12)
                )
            )
            .scalars()
            .all()
        )
        inyecciones = (
            (
                await session.execute(
                    select(Inyeccion)
                    .where(
                        Inyeccion.tenant_id == tid,
                        Inyeccion.cartera_id == cartera.id,
                        Inyeccion.deleted_at.is_(None),
                    )
                    .order_by(Inyeccion.fecha_hora.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        gastos = (
            (
                await session.execute(
                    select(Gasto)
                    .where(
                        Gasto.tenant_id == tid,
                        Gasto.cartera_id == cartera.id,
                        Gasto.deleted_at.is_(None),
                    )
                    .order_by(Gasto.fecha_hora.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        snapshots = (
            (
                await session.execute(
                    select(CarteraEstadisticasSnapshot)
                    .where(
                        CarteraEstadisticasSnapshot.tenant_id == tid,
                        CarteraEstadisticasSnapshot.cartera_id == cartera.id,
                    )
                    .order_by(CarteraEstadisticasSnapshot.fecha.desc())
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
        desde_mes = datetime.now(UTC) - timedelta(days=30)
        prestamo_ids = select(Prestamo.id).where(
            Prestamo.tenant_id == tid,
            Prestamo.cartera_id == cartera.id,
            Prestamo.deleted_at.is_(None),
        )
        mora = (
            await session.execute(
                select(func.coalesce(func.sum(Cuota.valor_esperado), 0)).where(
                    Cuota.tenant_id == tid,
                    Cuota.prestamo_id.in_(prestamo_ids),
                    Cuota.estatus == "atrasada",
                    Cuota.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        cobrado_30d = (
            await session.execute(
                select(func.coalesce(func.sum(Pago.valor), 0)).where(
                    Pago.tenant_id == tid,
                    Pago.prestamo_id.in_(prestamo_ids),
                    Pago.fecha_hora >= desde_mes,
                    Pago.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        gastos_total = (
            await session.execute(
                select(func.coalesce(func.sum(Gasto.valor), 0)).where(
                    Gasto.tenant_id == tid,
                    Gasto.cartera_id == cartera.id,
                    Gasto.estado == "activo",
                    Gasto.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        reportes = (
            (
                await session.execute(
                    select(ReporteDiario)
                    .where(
                        ReporteDiario.tenant_id == tid,
                        ReporteDiario.cartera_id == cartera.id,
                    )
                    .order_by(ReporteDiario.fecha.desc())
                    .limit(14)
                )
            )
            .scalars()
            .all()
        )
    return PortfolioDetail(
        id=cartera.id,
        nombre=cartera.nombre,
        descripcion=cartera.descripcion,
        gestor=f"{gestor.nombre} {gestor.apellido or ''}".strip() if gestor else None,
        tipo_interes=float(cartera.tipo_interes or 0),
        num_cuotas=int(cartera.num_cuotas or 1),
        moneda=cartera.moneda or "COP",
        estatus=cartera.estatus,
        clientes=int(clientes),
        prestamos=len(prestamos),
        saldo=float(sum((p.saldo for p in prestamos), Decimal(0))),
        mora=float(mora or 0),
        cobrado_30d=float(cobrado_30d or 0),
        gastos_total=float(gastos_total or 0),
        reportes_diarios=[
            ReporteDiarioOut(
                fecha=r.fecha.isoformat(),
                cobrado=float(r.cobrado or 0),
                prestado=float(r.prestado or 0),
                visitados=len(r.clientes_visitados or []),
                sin_pago=len(r.clientes_sin_pago or []),
                inicio=r.hora_inicio or "",
                fin=r.hora_fin or "",
            )
            for r in reportes
        ],
        caja=PortfolioCaja(
            saldo_inicial=float(caja.saldo_inicial) if caja else 0.0,
            saldo_actual=float(caja.saldo_actual) if caja else 0.0,
            actualizado=caja.updated_at.strftime("%Y-%m-%d %H:%M") if caja and caja.updated_at else "",
        ),
        liquidaciones=[
            Liquidation(
                cartera=cartera.nombre,
                fecha=liq.fecha,
                periodo=liq.periodo,
                cobrado=float(liq.total_cobrado or 0),
                gastos=float(liq.total_gastos or 0),
                comision=float(liq.comision_valor or 0),
            )
            for liq in liquidaciones
        ],
        inyecciones=[
            Injection(
                fecha=i.fecha_hora.strftime("%Y-%m-%d") if i.fecha_hora else "",
                valor=float(i.valor or 0),
                comentario=i.comentario or "",
                estado=i.estado,
            )
            for i in inyecciones
        ],
        gastos=[
            Expense(
                id=g.id,
                fecha=g.fecha_hora.strftime("%Y-%m-%d") if g.fecha_hora else "",
                descripcion=g.descripcion,
                valor=float(g.valor or 0),
                aprobado=bool(g.aprobado),
            )
            for g in gastos
        ],
        estadisticas=[
            EstadisticaSnapshot(fecha=snap.fecha, datos=snap.datos or {}) for snap in snapshots
        ],
    )


# ------------------------------------------------------- CRUD de carteras
class PortfolioIn(BaseModel):
    nombre: str
    descripcion: str = ""
    gestor_id: uuid.UUID | None = None
    tipo_interes: float = 0.0
    num_cuotas: int = 1
    moneda: str = "COP"
    costo_cobro: float = 0.0


def _as_portfolio(row: Cartera, gestor: str | None) -> Portfolio:
    return Portfolio(
        id=row.id,
        nombre=row.nombre,
        descripcion=row.descripcion,
        gestor=gestor,
        gestor_id=row.gestor_id,
        tipo_interes=float(row.tipo_interes or 0),
        num_cuotas=int(row.num_cuotas or 1),
        moneda=row.moneda or "COP",
        estatus=row.estatus,
        costo_cobro=float(row.costo_cobro or 0),
    )


async def _portfolio_values(
    session: Any, payload: PortfolioIn, *, tid: uuid.UUID
) -> dict[str, Any]:
    """Valida y normaliza el alta/edición de cartera (mismos campos que el clásico)."""
    nombre = payload.nombre.strip()
    if not nombre:
        raise ApiError.bad_request("El nombre es obligatorio.", "missing_name")
    if payload.gestor_id is not None and await _get_active(
        session, Gestor, payload.gestor_id, tid
    ) is None:
        raise ApiError.bad_request("El gestor indicado no existe.", "invalid_gestor")
    if payload.num_cuotas < 1:
        raise ApiError.bad_request("El número de cuotas debe ser al menos 1.", "invalid_installments")
    if payload.tipo_interes < 0 or payload.costo_cobro < 0:
        raise ApiError.bad_request("Interés y costo de cobro no pueden ser negativos.", "invalid_amount")
    return {
        "nombre": nombre,
        "descripcion": payload.descripcion.strip(),
        "gestor_id": payload.gestor_id,
        "tipo_interes": Decimal(str(payload.tipo_interes)),
        "num_cuotas": payload.num_cuotas,
        "moneda": payload.moneda.strip() or "COP",
        "costo_cobro": Decimal(str(payload.costo_cobro)),
    }


async def _gestor_nombre(session: Any, gestor_id: uuid.UUID | None) -> str | None:
    if gestor_id is None:
        return None
    gestor = (await session.execute(select(Gestor).where(Gestor.id == gestor_id))).scalar_one_or_none()
    return f"{gestor.nombre} {gestor.apellido or ''}".strip() if gestor else None


@router.post("/portfolios", response_model=Portfolio, status_code=201)
async def create_portfolio(
    payload: PortfolioIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Portfolio:
    tid = principal.tenant_id
    async with get_session() as session:
        values = await _portfolio_values(session, payload, tid=tid)
        cartera = Cartera(tenant_id=tid, **values)
        session.add(cartera)
        await session.flush()
        # Paridad con el clásico: cada cartera nace con su fila de configuración.
        session.add(CarteraConfig(tenant_id=tid, cartera_id=cartera.id))
        _audit(session, tid, "cartera", cartera.id, "crear", despues={"nombre": values["nombre"]})
        gestor = await _gestor_nombre(session, cartera.gestor_id)
        return _as_portfolio(cartera, gestor)


@router.put("/portfolios/{cartera_id}", response_model=Portfolio)
async def update_portfolio(
    cartera_id: uuid.UUID,
    payload: PortfolioIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Portfolio:
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        values = await _portfolio_values(session, payload, tid=tid)
        for campo, valor in values.items():
            setattr(cartera, campo, valor)
        _audit(session, tid, "cartera", cartera.id, "editar", despues={"nombre": values["nombre"]})
        return _as_portfolio(cartera, await _gestor_nombre(session, cartera.gestor_id))


@router.delete("/portfolios/{cartera_id}", status_code=204)
async def delete_portfolio(
    cartera_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Response:
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        cartera.deleted_at = datetime.now(UTC)
        _audit(session, tid, "cartera", cartera.id, "eliminar", despues={"nombre": cartera.nombre})
    return Response(status_code=204)


# ------------------------------------------------------- CRUD de gestores
class CollectorIn(BaseModel):
    nombre: str
    apellido: str = ""
    correo: str = ""
    telefono: str = ""
    codigo: str = ""
    password: str = ""


def _as_collector(row: Gestor) -> Collector:
    return Collector(
        id=row.id,
        nombre=f"{row.nombre} {row.apellido or ''}".strip(),
        correo=row.correo,
        telefono=row.telefono,
        codigo=row.codigo,
        estatus=row.estatus,
    )


def _collector_values(payload: CollectorIn) -> dict[str, str]:
    nombre = payload.nombre.strip()
    if not nombre:
        raise ApiError.bad_request("El nombre es obligatorio.", "missing_name")
    return {
        "nombre": nombre,
        "apellido": payload.apellido.strip(),
        "correo": (payload.correo or "").strip().lower(),
        "telefono": (payload.telefono or "").strip(),
        "codigo": (payload.codigo or "").strip(),
    }


@router.post("/collectors", status_code=201)
async def create_collector(
    payload: CollectorIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:gestores", write=True))],
) -> dict[str, Any]:
    """Alta del gestor: crea su cuenta del core (Admin + TenantUser) y su perfil (ADR-0066).

    Si la identidad ya existe (mismo correo), no se duplica: se crea una **invitación**
    que el usuario debe aceptar y se devuelve `{"resultado": "invitado"}` (ADR-0081).
    """
    tid = principal.tenant_id
    correo = (payload.correo or "").strip().lower()
    password = payload.password or ""
    if not correo or not password:
        raise ApiError.bad_request("Correo y contraseña son obligatorios.", "missing_credentials")
    if len(password) < 8:
        raise ApiError.bad_request("La contraseña debe tener al menos 8 caracteres.", "weak_password")
    async with get_session() as session:
        existente = (
            await session.execute(select(Admin).where(Admin.email == correo))
        ).scalar_one_or_none()
        if existente is not None:
            from core.admin.invitations_service import crear_invitacion
            from core.admin.roles import es_tester

            if es_tester(principal.admin):
                # Invitar una cuenta existente a un negocio es gestionar cuentas (ADR-0134).
                raise ApiError(409, "conflict", "Ese correo ya tiene una cuenta. Un tester solo crea gestores con una cuenta nueva.")
            error = await crear_invitacion(
                tenant_id=tid,
                admin_id=existente.id,
                invited_by=principal.admin.id,
                role="colaborador",
                pack_roles=["pack_cobranza:gestor"],
            )
            if error is not None:
                raise ApiError.bad_request(error, "invitation_error")
            return {"resultado": "invitado"}
        values = _collector_values(payload)
        admin = Admin(email=correo, password_hash=hash_password(password), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        gestor = Gestor(tenant_id=tid, admin_id=admin.id, **values)
        session.add(gestor)
        await session.flush()
        _audit(session, tid, "gestor", gestor.id, "crear", despues={"correo": correo})
        return {"resultado": "creado", "collector": _as_collector(gestor).model_dump()}


@router.put("/collectors/{collector_id}", response_model=Collector)
async def update_collector(
    collector_id: uuid.UUID,
    payload: CollectorIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:gestores", write=True))],
) -> Collector:
    tid = principal.tenant_id
    async with get_session() as session:
        gestor = await _get_active(session, Gestor, collector_id, tid)
        if gestor is None:
            raise ApiError.not_found("Gestor no encontrado.")
        for campo, valor in _collector_values(payload).items():
            setattr(gestor, campo, valor)
        _audit(session, tid, "gestor", gestor.id, "editar", despues={"correo": gestor.correo})
        return _as_collector(gestor)


@router.delete("/collectors/{collector_id}", status_code=204)
async def delete_collector(
    collector_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:gestores", write=True))],
) -> Response:
    tid = principal.tenant_id
    async with get_session() as session:
        gestor = await _get_active(session, Gestor, collector_id, tid)
        if gestor is None:
            raise ApiError.not_found("Gestor no encontrado.")
        gestor.deleted_at = datetime.now(UTC)
        _audit(session, tid, "gestor", gestor.id, "eliminar", despues={"correo": gestor.correo})
    return Response(status_code=204)


# ------------------------------------------------------- CRUD de clientes
RISK_COLORS = ("blanco", "azul", "rojo")


class ClientIn(BaseModel):
    nombre: str
    apellido: str = ""
    telefono: str = ""
    tipo_documento: str = ""
    numero_documento: str = ""
    negocio: str = ""
    cartera_id: uuid.UUID
    tope_maximo_deuda: float = 0.0
    color_riesgo: str = "blanco"


class ClientRow(BaseModel):
    id: uuid.UUID
    nombre: str
    apellido: str
    telefono: str
    tipo_documento: str
    numero_documento: str
    negocio: str
    cartera: str
    cartera_id: uuid.UUID
    tope_maximo_deuda: float
    color_riesgo: str


def _as_client(row: Cliente, cartera: str) -> ClientRow:
    return ClientRow(
        id=row.id,
        nombre=row.nombre,
        apellido=row.apellido or "",
        telefono=row.telefono_whatsapp or "",
        tipo_documento=row.tipo_documento or "",
        numero_documento=row.numero_documento or "",
        negocio=row.negocio or "",
        cartera=cartera,
        cartera_id=row.cartera_id,
        tope_maximo_deuda=float(row.tope_maximo_deuda or 0),
        color_riesgo=row.color_riesgo or "blanco",
    )


async def _client_values(session: Any, payload: ClientIn, *, tid: uuid.UUID) -> dict[str, Any]:
    nombre = payload.nombre.strip()
    if not nombre:
        raise ApiError.bad_request("El nombre es obligatorio.", "missing_name")
    if await _get_active(session, Cartera, payload.cartera_id, tid) is None:
        raise ApiError.bad_request("La cartera indicada no existe.", "invalid_portfolio")
    if payload.color_riesgo not in RISK_COLORS:
        raise ApiError.bad_request(
            f"Color inválido: {payload.color_riesgo}. Válidos: {', '.join(RISK_COLORS)}",
            "invalid_risk_color",
        )
    if payload.tope_maximo_deuda < 0:
        raise ApiError.bad_request("El tope de deuda no puede ser negativo.", "invalid_amount")
    return {
        "nombre": nombre,
        "apellido": payload.apellido.strip(),
        "telefono_whatsapp": payload.telefono.strip(),
        "tipo_documento": payload.tipo_documento.strip(),
        "numero_documento": payload.numero_documento.strip(),
        "negocio": payload.negocio.strip(),
        "cartera_id": payload.cartera_id,
        "tope_maximo_deuda": Decimal(str(payload.tope_maximo_deuda)),
        "color_riesgo": payload.color_riesgo,
    }


async def _cartera_nombre(session: Any, cartera_id: uuid.UUID) -> str:
    cartera = (
        await session.execute(select(Cartera).where(Cartera.id == cartera_id))
    ).scalar_one_or_none()
    return cartera.nombre if cartera else "—"


@router.get("/clients", response_model=list[ClientRow])
async def list_clients(
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes"))],
    cartera_id: uuid.UUID | None = None,
    q: str = "",
) -> list[ClientRow]:
    tid = principal.tenant_id
    query = select(Cliente).where(Cliente.tenant_id == tid, Cliente.deleted_at.is_(None))
    if cartera_id is not None:
        query = query.where(Cliente.cartera_id == cartera_id)
    texto = q.strip()
    if texto:
        like = f"%{texto}%"
        query = query.where(
            or_(
                Cliente.nombre.ilike(like),
                Cliente.apellido.ilike(like),
                Cliente.numero_documento.ilike(like),
            )
        )
    async with get_session() as session:
        rows = (
            (await session.execute(query.order_by(Cliente.nombre).limit(300))).scalars().all()
        )
        carteras = (
            (
                await session.execute(
                    select(Cartera).where(Cartera.tenant_id == tid)
                )
            )
            .scalars()
            .all()
        )
    nombres = {c.id: c.nombre for c in carteras}
    return [_as_client(c, nombres.get(c.cartera_id, "—")) for c in rows]


@router.post("/clients", response_model=ClientRow, status_code=201)
async def create_client(
    payload: ClientIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes", write=True))],
) -> ClientRow:
    tid = principal.tenant_id
    async with get_session() as session:
        values = await _client_values(session, payload, tid=tid)
        cliente = Cliente(tenant_id=tid, **values)
        session.add(cliente)
        await session.flush()
        _audit(session, tid, "cliente", cliente.id, "crear", despues={"nombre": values["nombre"]})
        return _as_client(cliente, await _cartera_nombre(session, cliente.cartera_id))


@router.put("/clients/{client_id}", response_model=ClientRow)
async def update_client(
    client_id: uuid.UUID,
    payload: ClientIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes", write=True))],
) -> ClientRow:
    tid = principal.tenant_id
    async with get_session() as session:
        cliente = await _get_active(session, Cliente, client_id, tid)
        if cliente is None:
            raise ApiError.not_found("Cliente no encontrado.")
        values = await _client_values(session, payload, tid=tid)
        for campo, valor in values.items():
            setattr(cliente, campo, valor)
        _audit(session, tid, "cliente", cliente.id, "editar", despues={"nombre": values["nombre"]})
        return _as_client(cliente, await _cartera_nombre(session, cliente.cartera_id))


@router.delete("/clients/{client_id}", status_code=204)
async def delete_client(
    client_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes", write=True))],
) -> Response:
    tid = principal.tenant_id
    async with get_session() as session:
        cliente = await _get_active(session, Cliente, client_id, tid)
        if cliente is None:
            raise ApiError.not_found("Cliente no encontrado.")
        cliente.deleted_at = datetime.now(UTC)
        _audit(session, tid, "cliente", cliente.id, "eliminar", despues={"nombre": cliente.nombre})
    return Response(status_code=204)


# ------------------------------------------------- ficha del cliente (3.2)
class Installment(BaseModel):
    id: uuid.UUID
    numero: int
    valor: float
    vence: date
    estatus: str


class Loan(BaseModel):
    id: uuid.UUID
    valor: float
    saldo: float
    num_cuotas: int
    tipo_interes: float
    fecha_otorgado: date
    estatus: str
    cuotas: list[Installment]


class ClientMovement(BaseModel):
    id: uuid.UUID
    tipo: str
    fecha: str
    valor: float
    metodo: str


class ClientDetail(BaseModel):
    cliente: ClientRow
    prestamos: list[Loan]
    movimientos: list[ClientMovement]
    cambios: list[ClientChange] = []


async def _caja_de(session: Any, tid: uuid.UUID, cartera_id: uuid.UUID) -> Caja:
    """Caja de la cartera, creándola si no existe (paridad con el clásico)."""
    caja = (
        await session.execute(
            select(Caja).where(Caja.tenant_id == tid, Caja.cartera_id == cartera_id)
        )
    ).scalar_one_or_none()
    if caja is None:
        caja = Caja(
            tenant_id=tid, cartera_id=cartera_id, saldo_inicial=Decimal(0), saldo_actual=Decimal(0)
        )
        session.add(caja)
        await session.flush()
    return caja


@router.get("/clients/{client_id}", response_model=ClientDetail)
async def client_detail(
    client_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes"))],
) -> ClientDetail:
    tid = principal.tenant_id
    async with get_session() as session:
        cliente = await _get_active(session, Cliente, client_id, tid)
        if cliente is None:
            raise ApiError.not_found("Cliente no encontrado.")
        prestamos = (
            (
                await session.execute(
                    select(Prestamo)
                    .where(
                        Prestamo.tenant_id == tid,
                        Prestamo.cliente_id == cliente.id,
                        Prestamo.deleted_at.is_(None),
                    )
                    .order_by(Prestamo.fecha_otorgado.desc())
                )
            )
            .scalars()
            .all()
        )
        cuotas = (
            (
                await session.execute(
                    select(Cuota)
                    .where(
                        Cuota.tenant_id == tid,
                        Cuota.prestamo_id.in_([p.id for p in prestamos]) if prestamos else False,
                        Cuota.deleted_at.is_(None),
                    )
                    .order_by(Cuota.numero_cuota)
                )
            )
            .scalars()
            .all()
        )
        pagos = (
            (
                await session.execute(
                    select(Pago)
                    .where(
                        Pago.tenant_id == tid,
                        Pago.cliente_id == cliente.id,
                        Pago.deleted_at.is_(None),
                    )
                    .order_by(Pago.fecha_hora.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        cambios = (
            (
                await session.execute(
                    select(CambioCliente)
                    .where(
                        CambioCliente.tenant_id == tid,
                        CambioCliente.cliente_id == cliente.id,
                        CambioCliente.estado == "pendiente",
                    )
                    .order_by(CambioCliente.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        abonos = (
            (
                await session.execute(
                    select(Abono)
                    .where(
                        Abono.tenant_id == tid,
                        Abono.cliente_id == cliente.id,
                        Abono.deleted_at.is_(None),
                    )
                    .order_by(Abono.fecha_hora.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        cartera = await _cartera_nombre(session, cliente.cartera_id)
    por_prestamo: dict[uuid.UUID, list[Installment]] = {}
    for c in cuotas:
        por_prestamo.setdefault(c.prestamo_id, []).append(
            Installment(
                id=c.id,
                numero=c.numero_cuota,
                valor=float(c.valor_esperado or 0),
                vence=c.fecha_vencimiento,
                estatus=c.estatus,
            )
        )
    movimientos = [
        ClientMovement(
            id=p.id,
            tipo="pago",
            fecha=p.fecha_hora.strftime("%Y-%m-%d %H:%M") if p.fecha_hora else "",
            valor=float(p.valor or 0),
            metodo=p.metodo_pago or "",
        )
        for p in pagos
    ] + [
        ClientMovement(
            id=a.id,
            tipo="abono",
            fecha=a.fecha_hora.strftime("%Y-%m-%d %H:%M") if a.fecha_hora else "",
            valor=float(a.valor or 0),
            metodo=a.metodo_pago or "",
        )
        for a in abonos
    ]
    movimientos.sort(key=lambda m: m.fecha, reverse=True)
    return ClientDetail(
        cliente=_as_client(cliente, cartera),
        cambios=[_as_change(c) for c in cambios],
        prestamos=[
            Loan(
                id=p.id,
                valor=float(p.valor or 0),
                saldo=float(p.saldo or 0),
                num_cuotas=int(p.num_cuotas or 0),
                tipo_interes=float(p.tipo_interes or 0),
                fecha_otorgado=p.fecha_otorgado,
                estatus=p.estatus,
                cuotas=por_prestamo.get(p.id, []),
            )
            for p in prestamos
        ],
        movimientos=movimientos[:20],
    )


class LoanIn(BaseModel):
    valor: float
    num_cuotas: int = 1
    tipo_interes: float = 0.0
    dias_entre_cuotas: int = 1
    fecha_otorgado: date | None = None


@router.post("/clients/{client_id}/loans", response_model=Loan, status_code=201)
async def create_loan(
    client_id: uuid.UUID,
    payload: LoanIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes", write=True))],
) -> Loan:
    """Crea el préstamo, su plan de cuotas y descuenta el desembolso de la caja."""
    tid = principal.tenant_id
    if payload.valor <= 0:
        raise ApiError.bad_request("El valor del préstamo debe ser mayor a cero.", "invalid_amount")
    if payload.num_cuotas < 1:
        raise ApiError.bad_request("El número de cuotas debe ser al menos 1.", "invalid_installments")
    if payload.tipo_interes < 0:
        raise ApiError.bad_request("El interés no puede ser negativo.", "invalid_rate")
    if payload.dias_entre_cuotas < 1:
        raise ApiError.bad_request("Los días entre cuotas deben ser al menos 1.", "invalid_days")
    fecha = payload.fecha_otorgado or date.today()
    async with get_session() as session:
        cliente = await _get_active(session, Cliente, client_id, tid)
        if cliente is None:
            raise ApiError.not_found("Cliente no encontrado.")
        valor = Decimal(str(payload.valor))
        interes = Decimal(str(payload.tipo_interes))
        total = (valor * (Decimal(1) + interes / Decimal(100))).quantize(Decimal("1"))
        cuota_valor = (total / payload.num_cuotas).quantize(Decimal("1"))
        prestamo = Prestamo(
            tenant_id=tid,
            cliente_id=cliente.id,
            cartera_id=cliente.cartera_id,
            valor=valor,
            saldo=total,
            num_cuotas=payload.num_cuotas,
            tipo_interes=interes,
            dias_entre_cuotas=payload.dias_entre_cuotas,
            fecha_otorgado=fecha,
        )
        session.add(prestamo)
        await session.flush()
        cuotas = [
            Cuota(
                tenant_id=tid,
                prestamo_id=prestamo.id,
                numero_cuota=i,
                valor_esperado=cuota_valor,
                fecha_vencimiento=fecha + timedelta(days=i * payload.dias_entre_cuotas),
            )
            for i in range(1, payload.num_cuotas + 1)
        ]
        session.add_all(cuotas)
        caja = await _caja_de(session, tid, cliente.cartera_id)
        caja.saldo_actual = (caja.saldo_actual or Decimal(0)) - valor
        _audit(
            session,
            tid,
            "prestamo",
            prestamo.id,
            "crear",
            despues={
                "valor": float(valor),
                "num_cuotas": payload.num_cuotas,
                "cliente_id": str(cliente.id),
            },
        )
        return Loan(
            id=prestamo.id,
            valor=float(prestamo.valor or 0),
            saldo=float(prestamo.saldo or 0),
            num_cuotas=prestamo.num_cuotas,
            tipo_interes=float(prestamo.tipo_interes or 0),
            fecha_otorgado=prestamo.fecha_otorgado,
            estatus=prestamo.estatus,
            cuotas=[
                Installment(
                    id=c.id,
                    numero=c.numero_cuota,
                    valor=float(c.valor_esperado or 0),
                    vence=c.fecha_vencimiento,
                    estatus=c.estatus,
                )
                for c in cuotas
            ],
        )


class PaymentIn(BaseModel):
    valor: float
    metodo_pago: str = "efectivo"
    cuota_id: uuid.UUID | None = None


@router.post("/loans/{loan_id}/payments", status_code=204)
async def register_loan_payment(
    loan_id: uuid.UUID,
    payload: PaymentIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes", write=True))],
) -> Response:
    """Registra el pago: marca la cuota, baja el saldo y suma a la caja."""
    if payload.valor <= 0:
        raise ApiError.bad_request("El valor del pago debe ser mayor a cero.", "invalid_amount")
    tid = principal.tenant_id
    async with get_session() as session:
        prestamo = await _get_active(session, Prestamo, loan_id, tid)
        if prestamo is None:
            raise ApiError.not_found("Préstamo no encontrado.")
        cuota = (
            await _get_active(session, Cuota, payload.cuota_id, tid) if payload.cuota_id else None
        )
        if payload.cuota_id and (cuota is None or cuota.prestamo_id != prestamo.id):
            raise ApiError.bad_request("La cuota no pertenece a este préstamo.", "invalid_installment")
        valor = Decimal(str(payload.valor))
        session.add(
            Pago(
                tenant_id=tid,
                cliente_id=prestamo.cliente_id,
                prestamo_id=prestamo.id,
                cuota_id=cuota.id if cuota else None,
                valor=valor,
                metodo_pago=payload.metodo_pago or "efectivo",
                registrado_por=principal.admin.email,
            )
        )
        if cuota is not None:
            cuota.estatus = "pagada"
        prestamo.saldo = (prestamo.saldo or Decimal(0)) - valor
        caja = await _caja_de(session, tid, prestamo.cartera_id)
        caja.saldo_actual = (caja.saldo_actual or Decimal(0)) + valor
        _audit(
            session,
            tid,
            "pago",
            prestamo.id,
            "crear",
            despues={
                "valor": float(valor),
                "metodo": payload.metodo_pago,
                "cuota_id": str(cuota.id) if cuota else None,
            },
        )
    return Response(status_code=204)


class CreditIn(BaseModel):
    valor: float
    metodo_pago: str = "efectivo"


@router.post("/loans/{loan_id}/credits", status_code=204)
async def register_loan_credit(
    loan_id: uuid.UUID,
    payload: CreditIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes", write=True))],
) -> Response:
    """Abono al préstamo sin cuota: baja el saldo y suma a la caja."""
    if payload.valor <= 0:
        raise ApiError.bad_request("El valor del abono debe ser mayor a cero.", "invalid_amount")
    tid = principal.tenant_id
    async with get_session() as session:
        prestamo = await _get_active(session, Prestamo, loan_id, tid)
        if prestamo is None:
            raise ApiError.not_found("Préstamo no encontrado.")
        valor = Decimal(str(payload.valor))
        session.add(
            Abono(
                tenant_id=tid,
                prestamo_id=prestamo.id,
                cliente_id=prestamo.cliente_id,
                valor=valor,
                metodo_pago=payload.metodo_pago or "efectivo",
                registrado_por=principal.admin.email,
            )
        )
        prestamo.saldo = (prestamo.saldo or Decimal(0)) - valor
        caja = await _caja_de(session, tid, prestamo.cartera_id)
        caja.saldo_actual = (caja.saldo_actual or Decimal(0)) + valor
        _audit(
            session,
            tid,
            "abono",
            prestamo.id,
            "crear",
            despues={"valor": float(valor), "metodo": payload.metodo_pago},
        )
    return Response(status_code=204)


# ---------------------------------------- cuotas y abonos (cierre de 3.2)
class InstallmentIn(BaseModel):
    valor: float
    vence: date
    estatus: str = "pendiente"


INSTALLMENT_STATUSES = ("pendiente", "pagada", "atrasada", "parcial")


@router.put("/installments/{installment_id}", response_model=Installment)
async def update_installment(
    installment_id: uuid.UUID,
    payload: InstallmentIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes", write=True))],
) -> Installment:
    if payload.valor <= 0:
        raise ApiError.bad_request("El valor de la cuota debe ser mayor a cero.", "invalid_amount")
    estatus = payload.estatus if payload.estatus in INSTALLMENT_STATUSES else "pendiente"
    tid = principal.tenant_id
    async with get_session() as session:
        cuota = await _get_active(session, Cuota, installment_id, tid)
        if cuota is None:
            raise ApiError.not_found("Cuota no encontrada.")
        antes = {
            "valor": float(cuota.valor_esperado or 0),
            "vence": cuota.fecha_vencimiento.isoformat(),
            "estatus": cuota.estatus,
        }
        cuota.valor_esperado = Decimal(str(payload.valor))
        cuota.fecha_vencimiento = payload.vence
        cuota.estatus = estatus
        _audit(
            session,
            tid,
            "cuota",
            cuota.id,
            "editar",
            antes=antes,
            despues={"valor": payload.valor, "vence": payload.vence.isoformat(), "estatus": estatus},
        )
        return Installment(
            id=cuota.id,
            numero=cuota.numero_cuota,
            valor=float(cuota.valor_esperado or 0),
            vence=cuota.fecha_vencimiento,
            estatus=cuota.estatus,
        )


@router.delete("/installments/{installment_id}", status_code=204)
async def delete_installment(
    installment_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes", write=True))],
) -> Response:
    tid = principal.tenant_id
    async with get_session() as session:
        cuota = await _get_active(session, Cuota, installment_id, tid)
        if cuota is None:
            raise ApiError.not_found("Cuota no encontrada.")
        cuota.deleted_at = datetime.now(UTC)
        _audit(
            session,
            tid,
            "cuota",
            cuota.id,
            "eliminar",
            antes={"valor": float(cuota.valor_esperado or 0), "numero": cuota.numero_cuota},
        )
    return Response(status_code=204)


@router.delete("/credits/{credit_id}", status_code=204)
async def delete_credit(
    credit_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes", write=True))],
) -> Response:
    tid = principal.tenant_id
    async with get_session() as session:
        abono = await _get_active(session, Abono, credit_id, tid)
        if abono is None:
            raise ApiError.not_found("Abono no encontrado.")
        abono.deleted_at = datetime.now(UTC)
        _audit(
            session,
            tid,
            "abono",
            abono.id,
            "eliminar",
            antes={"valor": float(abono.valor or 0)},
        )
    return Response(status_code=204)


# ------------------------------------------------------------ caja (3.3)
class CashAdjustment(BaseModel):
    valor_nuevo: float
    motivo: str


@router.get("/portfolios/{cartera_id}/cash", response_model=PortfolioCaja)
async def get_cash(
    cartera_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras"))],
) -> PortfolioCaja:
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        caja = (
            await session.execute(
                select(Caja).where(Caja.tenant_id == tid, Caja.cartera_id == cartera.id)
            )
        ).scalar_one_or_none()
    return PortfolioCaja(
        saldo_inicial=float(caja.saldo_inicial) if caja else 0.0,
        saldo_actual=float(caja.saldo_actual) if caja else 0.0,
        actualizado=caja.updated_at.strftime("%Y-%m-%d %H:%M") if caja and caja.updated_at else "",
    )


@router.post("/portfolios/{cartera_id}/cash/adjustment", status_code=204)
async def adjust_cash(
    cartera_id: uuid.UUID,
    payload: CashAdjustment,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Response:
    """Ajuste manual de caja: deja el saldo en el valor indicado y registra el motivo."""
    motivo = payload.motivo.strip()
    if not motivo:
        raise ApiError.bad_request("Indica el motivo del ajuste.", "missing_reason")
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        caja = await _caja_de(session, tid, cartera.id)
        anterior = caja.saldo_actual or Decimal(0)
        nuevo = Decimal(str(payload.valor_nuevo))
        caja.saldo_actual = nuevo
        session.add(
            CajaAjuste(
                tenant_id=tid,
                caja_id=caja.id,
                valor_anterior=anterior,
                valor_nuevo=nuevo,
                motivo=motivo,
                actor_rol="admin",
            )
        )
        _audit(
            session,
            tid,
            "caja",
            caja.id,
            "editar",
            antes={"saldo": float(anterior)},
            despues={"saldo": float(nuevo)},
            motivo=motivo,
        )
    return Response(status_code=204)


@router.post("/portfolios/{cartera_id}/cash/closing", status_code=204)
async def close_cash(
    cartera_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Response:
    """Cierre del día: genera el snapshot de la cartera (liquidación)."""
    from pack_cobranza.jobs import snapshot_cartera

    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        await snapshot_cartera(session, tid, cartera)
    return Response(status_code=204)


@router.post("/portfolios/{cartera_id}/cash/opening", status_code=204)
async def open_cash(
    cartera_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Response:
    """Apertura del día: marca la hora de inicio de la jornada de la cartera."""
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        session.add(
            CarteraApertura(
                tenant_id=tid,
                cartera_id=cartera.id,
                fecha=date.today(),
                hora_inicio=datetime.now(UTC).strftime("%H:%M"),
            )
        )
    return Response(status_code=204)


class PositionIn(BaseModel):
    lat: float
    lng: float


@router.post("/portfolios/{cartera_id}/position", status_code=204)
async def register_position(
    cartera_id: uuid.UUID,
    payload: PositionIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Response:
    """Posición del gestor (se usa desde la app de campo; aquí queda disponible)."""
    if not (-90 <= payload.lat <= 90) or not (-180 <= payload.lng <= 180):
        raise ApiError.bad_request("Coordenadas fuera de rango.", "invalid_coordinates")
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        session.add(
            PosicionGestor(
                tenant_id=tid, cartera_id=cartera.id, lat=payload.lat, lng=payload.lng
            )
        )
    return Response(status_code=204)


class InjectionIn(BaseModel):
    valor: float
    comentario: str = ""


@router.post("/portfolios/{cartera_id}/injections", response_model=Injection, status_code=201)
async def create_injection(
    cartera_id: uuid.UUID,
    payload: InjectionIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Injection:
    """Aporte de capital a la cartera: crea la inyección y suma a la caja."""
    if payload.valor <= 0:
        raise ApiError.bad_request("El valor de la inyección debe ser mayor a cero.", "invalid_amount")
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        valor = Decimal(str(payload.valor))
        inyeccion = Inyeccion(
            tenant_id=tid,
            cartera_id=cartera.id,
            valor=valor,
            comentario=payload.comentario.strip() or None,
        )
        session.add(inyeccion)
        await session.flush()
        caja = await _caja_de(session, tid, cartera.id)
        caja.saldo_actual = (caja.saldo_actual or Decimal(0)) + valor
        _audit(
            session,
            tid,
            "inyeccion",
            inyeccion.id,
            "crear",
            despues={"valor": float(valor)},
            motivo="Aporte de capital",
        )
        return Injection(
            fecha=inyeccion.fecha_hora.strftime("%Y-%m-%d") if inyeccion.fecha_hora else "",
            valor=float(inyeccion.valor or 0),
            comentario=inyeccion.comentario or "",
            estado=inyeccion.estado,
        )


class LiquidationIn(BaseModel):
    periodo: str = "diario"


LIQUIDATION_PERIODS = ("diario", "semanal", "quincenal", "mensual")


@router.post("/portfolios/{cartera_id}/liquidations", response_model=Liquidation, status_code=201)
async def create_liquidation(
    cartera_id: uuid.UUID,
    payload: LiquidationIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Liquidation:
    """Genera la liquidación del período (misma cuenta que el panel clásico)."""
    periodo = payload.periodo if payload.periodo in LIQUIDATION_PERIODS else "diario"
    hoy = date.today()
    desde = {
        "diario": hoy,
        "semanal": hoy - timedelta(days=7),
        "quincenal": hoy - timedelta(days=15),
        "mensual": hoy - timedelta(days=30),
    }[periodo]
    desde_dt = datetime.combine(desde, datetime.min.time(), tzinfo=UTC)
    hoy_dt = datetime.combine(hoy, datetime.min.time(), tzinfo=UTC)
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        prestamo_ids = select(Prestamo.id).where(
            Prestamo.tenant_id == tid, Prestamo.cartera_id == cartera.id
        )
        cobrado = Decimal(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Pago.valor), 0)).where(
                        Pago.tenant_id == tid,
                        Pago.prestamo_id.in_(prestamo_ids),
                        Pago.fecha_hora >= desde_dt,
                        Pago.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            or 0
        )
        prestado = Decimal(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Prestamo.valor), 0)).where(
                        Prestamo.tenant_id == tid,
                        Prestamo.cartera_id == cartera.id,
                        Prestamo.fecha_otorgado >= desde,
                        Prestamo.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            or 0
        )
        gastos = Decimal(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Gasto.valor), 0)).where(
                        Gasto.tenant_id == tid,
                        Gasto.cartera_id == cartera.id,
                        Gasto.estado == "activo",
                        Gasto.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            or 0
        )
        inyeccion = Decimal(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Inyeccion.valor), 0)).where(
                        Inyeccion.tenant_id == tid,
                        Inyeccion.cartera_id == cartera.id,
                        Inyeccion.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            or 0
        )
        suma_cartera = Decimal(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Prestamo.saldo), 0)).where(
                        Prestamo.tenant_id == tid,
                        Prestamo.cartera_id == cartera.id,
                        Prestamo.estatus == "vigente",
                        Prestamo.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            or 0
        )
        estimado = Decimal(
            (
                await session.execute(
                    select(func.coalesce(func.sum(Cuota.valor_esperado), 0)).where(
                        Cuota.tenant_id == tid,
                        Cuota.prestamo_id.in_(prestamo_ids),
                        Cuota.estatus.in_(("pendiente", "atrasada")),
                        Cuota.fecha_vencimiento <= hoy,
                        Cuota.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            or 0
        )
        cfg = (
            await session.execute(
                select(CarteraConfig).where(
                    CarteraConfig.tenant_id == tid, CarteraConfig.cartera_id == cartera.id
                )
            )
        ).scalar_one_or_none()
        comision_pct = cfg.comision_porcentaje if cfg is not None else Decimal(0)
        comision = (cobrado * comision_pct / Decimal(100)).quantize(Decimal("1"))
        caja = await _caja_de(session, tid, cartera.id)
        clientes = (
            (
                await session.execute(
                    select(Cliente).where(
                        Cliente.tenant_id == tid,
                        Cliente.cartera_id == cartera.id,
                        Cliente.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        vigentes = (
            (
                await session.execute(
                    select(Prestamo.cliente_id).where(
                        Prestamo.tenant_id == tid,
                        Prestamo.cartera_id == cartera.id,
                        Prestamo.estatus == "vigente",
                        Prestamo.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        con_prestamo = set(vigentes)
        visitas = (
            await session.execute(
                select(Visita.cliente_id, Visita.resultado).where(
                    Visita.tenant_id == tid,
                    Visita.cartera_id == cartera.id,
                    Visita.fecha == hoy,
                    Visita.deleted_at.is_(None),
                )
            )
        ).all()
        pagaron = {v.cliente_id for v in visitas if v.resultado == "pago"}
        pagos_hoy = (
            (
                await session.execute(
                    select(Pago.fecha_hora)
                    .where(
                        Pago.tenant_id == tid,
                        Pago.prestamo_id.in_(prestamo_ids),
                        Pago.fecha_hora >= hoy_dt,
                        Pago.deleted_at.is_(None),
                    )
                    .order_by(Pago.fecha_hora)
                )
            )
            .scalars()
            .all()
        )
        horas = [pg.strftime("%H:%M") for pg in pagos_hoy if pg is not None]
        detalle = {
            "pagaron": len(pagaron),
            "faltan": max(len(con_prestamo - pagaron), 0),
            "totalDia": len(con_prestamo),
            "sinCuentas": max(len(clientes) - len(con_prestamo), 0),
            "horaPrimera": horas[0] if horas else None,
            "horaUltima": horas[-1] if horas else None,
        }
        liquidacion = Liquidacion(
            tenant_id=tid,
            cartera_id=cartera.id,
            fecha=hoy,
            periodo=periodo,
            caja_anterior=caja.saldo_inicial,
            caja_actual=caja.saldo_actual,
            total_cobrado=cobrado,
            total_prestado=prestado,
            total_gastos=gastos,
            comision_porcentaje=comision_pct,
            comision_valor=comision,
            estimado_acobrar=estimado,
            total_inyeccion=inyeccion,
            suma_cartera=suma_cartera,
            detalle=detalle,
        )
        session.add(liquidacion)
        await session.flush()
        return Liquidation(
            cartera=cartera.nombre,
            fecha=liquidacion.fecha,
            periodo=liquidacion.periodo,
            cobrado=float(liquidacion.total_cobrado or 0),
            gastos=float(liquidacion.total_gastos or 0),
            comision=float(liquidacion.comision_valor or 0),
        )


class ExpenseApproval(BaseModel):
    """Sin cuerpo: la aprobación no lleva datos extra (paridad con el clásico)."""


@router.post("/expenses/{expense_id}/approve", status_code=204)
async def approve_expense(
    expense_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Response:
    """Aprueba el gasto: descuenta su valor de la caja de la cartera (una sola vez)."""
    tid = principal.tenant_id
    async with get_session() as session:
        gasto = await _get_active(session, Gasto, expense_id, tid)
        if gasto is None:
            raise ApiError.not_found("Gasto no encontrado.")
        if gasto.aprobado:
            return Response(status_code=204)
        gasto.aprobado = True
        caja = await _caja_de(session, tid, gasto.cartera_id)
        caja.saldo_actual = (caja.saldo_actual or Decimal(0)) - (gasto.valor or Decimal(0))
        _audit(
            session,
            tid,
            "gasto",
            gasto.id,
            "editar",
            antes={"aprobado": False},
            despues={"aprobado": True},
            motivo="Gasto aprobado",
        )
    return Response(status_code=204)


class PaymentLinkIn(BaseModel):
    referencia: str
    url: str


@router.post("/owner-charges/{charge_id}/links", response_model=PaymentLink, status_code=201)
async def create_payment_link(
    charge_id: uuid.UUID,
    payload: PaymentLinkIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:cobros", write=True))],
) -> PaymentLink:
    """Crea un link de pago para el cobro del propietario (monto = cobro calculado)."""
    referencia = payload.referencia.strip()
    url = payload.url.strip()
    if not referencia or not url:
        raise ApiError.bad_request("Referencia y URL son obligatorias.", "missing_fields")
    if not url.startswith(("http://", "https://")):
        raise ApiError.bad_request("La URL debe empezar por http:// o https://.", "invalid_url")
    tid = principal.tenant_id
    async with get_session() as session:
        cobro = await _get_active(session, CobroPropietario, charge_id, tid)
        if cobro is None:
            raise ApiError.not_found("Cobro no encontrado.")
        link = LinkPago(
            tenant_id=tid,
            cobro_id=cobro.id,
            referencia=referencia,
            url=url,
            monto=cobro.monto_calculado,
        )
        session.add(link)
        await session.flush()
        return PaymentLink(
            referencia=link.referencia,
            url=link.url,
            monto=float(link.monto or 0),
            estado=link.estado,
        )


class ExpenseIn(BaseModel):
    descripcion: str
    valor: float


class ExpenseCreate(ExpenseIn):
    cartera_id: uuid.UUID


def _as_expense(row: Gasto) -> Expense:
    return Expense(
        id=row.id,
        fecha=row.fecha_hora.strftime("%Y-%m-%d") if row.fecha_hora else "",
        descripcion=row.descripcion,
        valor=float(row.valor or 0),
        aprobado=bool(row.aprobado),
    )


def _expense_values(payload: ExpenseIn) -> dict[str, Any]:
    descripcion = payload.descripcion.strip()
    if not descripcion:
        raise ApiError.bad_request("La descripción es obligatoria.", "missing_description")
    if payload.valor <= 0:
        raise ApiError.bad_request("El valor del gasto debe ser mayor a cero.", "invalid_amount")
    return {"descripcion": descripcion, "valor": Decimal(str(payload.valor))}


@router.post("/expenses", response_model=Expense, status_code=201)
async def create_expense(
    payload: ExpenseCreate,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Expense:
    """Registra un gasto de la cartera (nace sin aprobar: no toca la caja hasta aprobarse)."""
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, payload.cartera_id, tid)
        if cartera is None:
            raise ApiError.bad_request("La cartera indicada no existe.", "invalid_portfolio")
        values = _expense_values(payload)
        gasto = Gasto(
            tenant_id=tid,
            cartera_id=cartera.id,
            descripcion=values["descripcion"],
            valor=values["valor"],
            aprobado=False,
        )
        session.add(gasto)
        await session.flush()
        return _as_expense(gasto)


@router.put("/expenses/{expense_id}", response_model=Expense)
async def update_expense(
    expense_id: uuid.UUID,
    payload: ExpenseIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Expense:
    """Edita un gasto SIN aprobar.

    El clásico permite editar también los aprobados, pero eso descuadra la caja
    (el valor descontado no se ajusta): aquí se bloquea a propósito.
    """
    tid = principal.tenant_id
    async with get_session() as session:
        gasto = await _get_active(session, Gasto, expense_id, tid)
        if gasto is None:
            raise ApiError.not_found("Gasto no encontrado.")
        if gasto.aprobado:
            raise ApiError.bad_request(
                "El gasto ya está aprobado: no se puede editar (descuadraría la caja).",
                "expense_approved",
            )
        values = _expense_values(payload)
        gasto.descripcion = values["descripcion"]
        gasto.valor = values["valor"]
        return _as_expense(gasto)


# ------------------------------- configuración de cartera y notas (3.5)
# (nombre, tipo) — espejo de _CONFIG_FIELDS del panel clásico.
CARTERA_CONFIG_FIELDS: list[tuple[str, str]] = [
    ("cuotas_minimas_prestamo", "int"),
    ("cuotas_atraso_umbral", "int"),
    ("manejo_cupo_activo", "bool"),
    ("cupo_default", "num"),
    ("recargo_activo", "bool"),
    ("bloquear_cambio_interes", "bool"),
    ("comision_activa", "bool"),
    ("comision_porcentaje", "num"),
    ("mostrar_fecha_ultima_liquidada", "bool"),
    ("mostrar_caja", "bool"),
    ("mostrar_cobrado_liquidada", "bool"),
    ("mostrar_prestamos", "bool"),
    ("mostrar_cobro_estimado", "bool"),
    ("ocultar_cartera", "bool"),
    ("eliminar_prestamos_apk", "bool"),
    ("eliminar_pagos_apk", "bool"),
    ("eliminar_abonos_apk", "bool"),
    ("eliminar_gastos_apk", "bool"),
    ("eliminar_inyeccion_apk", "bool"),
    ("registrar_inyeccion_apk", "bool"),
    ("generar_reportes_apk", "bool"),
    ("reconocimiento_facial_activo", "bool"),
    ("registro_documento_cliente", "bool"),
    ("bloqueo_automatico_clientes", "bool"),
    ("borrar_clientes_sin_deuda", "bool"),
    # Campos del clásico que faltaban en la API: sin esto el panel nuevo no podía
    # leerlos ni guardarlos (el guardado los dejaba intactos, pero eran invisibles).
    ("permitir_cambio_fecha_prestamo", "bool"),
    ("dias_no_laborables", "text"),
    ("periodo_liquidacion", "text"),
    ("dias_anticipacion_notificacion", "int"),
    ("aviso_dia_cobro", "bool"),
    ("umbral_mora_notificacion", "int"),
    ("ruta_habilitada", "bool"),
]


class PortfolioConfig(BaseModel):
    values: dict[str, Any]


def _config_values(cfg: CarteraConfig | None) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for nombre, tipo in CARTERA_CONFIG_FIELDS:
        actual = getattr(cfg, nombre, None) if cfg is not None else None
        if tipo == "bool":
            values[nombre] = bool(actual) if actual is not None else False
        elif tipo == "int":
            values[nombre] = int(actual) if actual is not None else 0
        elif tipo == "text":
            values[nombre] = str(actual) if actual is not None else ""
        else:
            values[nombre] = float(actual) if actual is not None else 0.0
    return values


@router.get("/portfolios/{cartera_id}/config", response_model=PortfolioConfig)
async def get_portfolio_config(
    cartera_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras"))],
) -> PortfolioConfig:
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        cfg = (
            await session.execute(
                select(CarteraConfig).where(
                    CarteraConfig.tenant_id == tid, CarteraConfig.cartera_id == cartera.id
                )
            )
        ).scalar_one_or_none()
        return PortfolioConfig(values=_config_values(cfg))


@router.put("/portfolios/{cartera_id}/config", response_model=PortfolioConfig)
async def save_portfolio_config(
    cartera_id: uuid.UUID,
    payload: PortfolioConfig,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> PortfolioConfig:
    """Guarda la configuración de la cartera (misma coerción de tipos que el clásico)."""
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        cfg = (
            await session.execute(
                select(CarteraConfig).where(
                    CarteraConfig.tenant_id == tid, CarteraConfig.cartera_id == cartera.id
                )
            )
        ).scalar_one_or_none()
        if cfg is None:
            cfg = CarteraConfig(tenant_id=tid, cartera_id=cartera.id)
            session.add(cfg)
        for nombre, tipo in CARTERA_CONFIG_FIELDS:
            if nombre not in payload.values:
                continue
            bruto = payload.values[nombre]
            if tipo == "bool":
                setattr(cfg, nombre, bool(bruto))
            elif tipo == "text":
                setattr(cfg, nombre, str(bruto or ""))
            elif tipo == "int":
                try:
                    setattr(cfg, nombre, int(bruto or 0))
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    setattr(cfg, nombre, Decimal(str(bruto or 0)))
                except Exception:
                    continue
        await session.flush()
        return PortfolioConfig(values=_config_values(cfg))


class NoteIn(BaseModel):
    nota: str


class NoteRow(BaseModel):
    id: uuid.UUID
    nota: str
    fecha: str
    autor: str


def _as_note(row: CarteraNota) -> NoteRow:
    return NoteRow(
        id=row.id,
        nota=row.nota,
        fecha=row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        autor=row.creado_por_rol or "",
    )


@router.get("/portfolios/{cartera_id}/notes", response_model=list[NoteRow])
async def list_notes(
    cartera_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras"))],
) -> list[NoteRow]:
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        notas = (
            (
                await session.execute(
                    select(CarteraNota)
                    .where(
                        CarteraNota.tenant_id == tid,
                        CarteraNota.cartera_id == cartera.id,
                        CarteraNota.deleted_at.is_(None),
                    )
                    .order_by(CarteraNota.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    return [_as_note(n) for n in notas]


@router.post("/portfolios/{cartera_id}/notes", response_model=NoteRow, status_code=201)
async def create_note(
    cartera_id: uuid.UUID,
    payload: NoteIn,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> NoteRow:
    nota = payload.nota.strip()
    if not nota:
        raise ApiError.bad_request("La nota no puede estar vacía.", "missing_note")
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        row = CarteraNota(
            tenant_id=tid, cartera_id=cartera.id, nota=nota, creado_por_rol="admin"
        )
        session.add(row)
        await session.flush()
        return _as_note(row)


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(
    note_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> Response:
    tid = principal.tenant_id
    async with get_session() as session:
        nota = await _get_active(session, CarteraNota, note_id, tid)
        if nota is None:
            raise ApiError.not_found("Nota no encontrada.")
        nota.deleted_at = datetime.now(UTC)
    return Response(status_code=204)


# ------------------------------------------------ alta de dispositivos (3.5)
class DeviceCreate(BaseModel):
    gestor_id: uuid.UUID | None = None
    cartera_id: uuid.UUID | None = None
    imei: str = ""
    whatsapp: str = ""


class DeviceCreated(BaseModel):
    codigo: str
    api_key: str
    gestor: str | None = None


@router.post("/devices", response_model=DeviceCreated, status_code=201)
async def create_device(
    payload: DeviceCreate,
    principal: Annotated[Principal, Depends(require("pack_cobranza:dispositivos", write=True))],
) -> DeviceCreated:
    """Genera un dispositivo para la app de campo.

    El `codigo` y la `api_key` se devuelven **una sola vez**: en la base solo queda
    el hash de la API key (paridad con el clásico, ADR-0057).
    """
    tid = principal.tenant_id
    async with get_session() as session:
        gestor = None
        if payload.gestor_id is not None:
            gestor = await _get_active(session, Gestor, payload.gestor_id, tid)
            if gestor is None:
                raise ApiError.bad_request("El gestor indicado no existe.", "invalid_collector")
        if payload.cartera_id is not None and await _get_active(
            session, Cartera, payload.cartera_id, tid
        ) is None:
            raise ApiError.bad_request("La cartera indicada no existe.", "invalid_portfolio")
        codigo = str(uuid.uuid4())
        api_key = secrets.token_urlsafe(32)
        session.add(
            Device(
                tenant_id=tid,
                codigo=codigo,
                api_key_hash=hash_password(api_key),
                gestor_id=gestor.id if gestor is not None else None,
                cartera_id=payload.cartera_id,
                imei=(payload.imei or "").strip() or None,
                whatsapp_number=(payload.whatsapp or "").strip() or None,
            )
        )
        return DeviceCreated(
            codigo=codigo,
            api_key=api_key,
            gestor=f"{gestor.nombre} {gestor.apellido or ''}".strip() if gestor else None,
        )


# ------------------------------------------- permisos del gestor (3.5)
class CollectorPermissions(BaseModel):
    catalogo: list[str]
    activos: list[str]


async def _permisos_de(session: Any, tid: uuid.UUID, gestor_id: uuid.UUID) -> list[str]:
    rows = (
        (
            await session.execute(
                select(GestorPermiso).where(
                    GestorPermiso.tenant_id == tid, GestorPermiso.gestor_id == gestor_id
                )
            )
        )
        .scalars()
        .all()
    )
    return [row.permiso for row in rows if row.habilitado]


@router.get("/collectors/{collector_id}/permissions", response_model=CollectorPermissions)
async def get_collector_permissions(
    collector_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:gestores"))],
) -> CollectorPermissions:
    tid = principal.tenant_id
    async with get_session() as session:
        gestor = await _get_active(session, Gestor, collector_id, tid)
        if gestor is None:
            raise ApiError.not_found("Gestor no encontrado.")
        activos = await _permisos_de(session, tid, gestor.id)
    return CollectorPermissions(catalogo=list(GESTOR_PERMISOS), activos=activos)


class CollectorPermissionsUpdate(BaseModel):
    permisos: list[str]


@router.put("/collectors/{collector_id}/permissions", response_model=CollectorPermissions)
async def save_collector_permissions(
    collector_id: uuid.UUID,
    payload: CollectorPermissionsUpdate,
    principal: Annotated[Principal, Depends(require("pack_cobranza:gestores", write=True))],
) -> CollectorPermissions:
    """Marca/desmarca la matriz del gestor (14 permisos; los desconocidos se ignoran)."""
    marcados = {p for p in payload.permisos if p in GESTOR_PERMISOS}
    tid = principal.tenant_id
    async with get_session() as session:
        gestor = await _get_active(session, Gestor, collector_id, tid)
        if gestor is None:
            raise ApiError.not_found("Gestor no encontrado.")
        existentes = (
            (
                await session.execute(
                    select(GestorPermiso).where(
                        GestorPermiso.tenant_id == tid, GestorPermiso.gestor_id == gestor.id
                    )
                )
            )
            .scalars()
            .all()
        )
        por_permiso = {row.permiso: row for row in existentes}
        for permiso in GESTOR_PERMISOS:
            row = por_permiso.get(permiso)
            if row is None:
                session.add(
                    GestorPermiso(
                        tenant_id=tid,
                        gestor_id=gestor.id,
                        permiso=permiso,
                        habilitado=permiso in marcados,
                    )
                )
            else:
                row.habilitado = permiso in marcados
        await session.flush()
        activos = await _permisos_de(session, tid, gestor.id)
    return CollectorPermissions(catalogo=list(GESTOR_PERMISOS), activos=activos)


# --------------------------------------------- trayectoria del día (3.5)
class RouteClient(BaseModel):
    id: uuid.UUID
    cliente: str
    telefono: str
    saldo: float
    mora: int
    riesgo: str
    lat: float | None
    lng: float | None


class RoutePlan(BaseModel):
    orden: list[uuid.UUID]
    distancia_km: float
    tiempo_min: int
    fecha: str


class RouteOut(BaseModel):
    clientes: list[RouteClient]
    plan: RoutePlan | None


async def _clientes_del_dia(
    session: Any, tid: uuid.UUID, cartera_id: uuid.UUID
) -> list[RouteClient]:
    """Clientes a visitar: préstamo vigente con cuotas pendientes o atrasadas."""
    clientes = (
        (
            await session.execute(
                select(Cliente).where(
                    Cliente.tenant_id == tid,
                    Cliente.cartera_id == cartera_id,
                    Cliente.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    out: list[RouteClient] = []
    hoy = date.today()
    for c in clientes:
        prestamos = (
            (
                await session.execute(
                    select(Prestamo.id).where(
                        Prestamo.tenant_id == tid,
                        Prestamo.cliente_id == c.id,
                        Prestamo.estatus == "vigente",
                        Prestamo.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not prestamos:
            continue
        cuotas = (
            (
                await session.execute(
                    select(Cuota).where(
                        Cuota.tenant_id == tid,
                        Cuota.prestamo_id.in_(prestamos),
                        Cuota.estatus.in_(("pendiente", "atrasada")),
                        Cuota.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        saldo = sum((q.valor_esperado for q in cuotas), Decimal(0))
        mora = max(
            ((hoy - q.fecha_vencimiento).days for q in cuotas if q.fecha_vencimiento < hoy),
            default=0,
        )
        out.append(
            RouteClient(
                id=c.id,
                cliente=f"{c.nombre} {c.apellido or ''}".strip(),
                telefono=c.telefono_whatsapp or "",
                saldo=float(saldo),
                mora=mora,
                riesgo=c.color_riesgo or "blanco",
                lat=float(c.lat) if c.lat is not None else None,
                lng=float(c.lng) if c.lng is not None else None,
            )
        )
    out.sort(key=lambda r: r.mora, reverse=True)
    return out


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371.0
    dlat = radians(b[0] - a[0])
    dlng = radians(b[1] - a[1])
    h = sin(dlat / 2) ** 2 + cos(radians(a[0])) * cos(radians(b[0])) * sin(dlng / 2) ** 2
    return 2 * r * asin(sqrt(h))


def _ordenar_trayecto(
    clientes: list[RouteClient], inicio: tuple[float, float] | None
) -> tuple[list[uuid.UUID], float]:
    """Orden de visita por vecino más cercano (misma heurística que el clásico)."""
    puntos = [c for c in clientes if c.lat is not None and c.lng is not None]
    if not puntos:
        return [], 0.0
    restantes = list(puntos)
    orden: list[uuid.UUID] = []
    km = 0.0
    if inicio is None:
        primero = restantes.pop(0)
        orden.append(primero.id)
        actual = (primero.lat or 0.0, primero.lng or 0.0)
    else:
        actual = inicio
    while restantes:
        sig = min(restantes, key=lambda c: _haversine_km(actual, (c.lat or 0.0, c.lng or 0.0)))
        km += _haversine_km(actual, (sig.lat or 0.0, sig.lng or 0.0))
        orden.append(sig.id)
        actual = (sig.lat or 0.0, sig.lng or 0.0)
        restantes.remove(sig)
    return orden, km


async def _ultimo_plan(session: Any, tid: uuid.UUID, cartera_id: uuid.UUID) -> RoutePlan | None:
    row = (
        await session.execute(
            select(CarteraOptimizadaLog)
            .where(
                CarteraOptimizadaLog.tenant_id == tid,
                CarteraOptimizadaLog.cartera_id == cartera_id,
            )
            .order_by(CarteraOptimizadaLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return RoutePlan(
        orden=[uuid.UUID(str(c)) for c in (row.orden_clientes or [])],
        distancia_km=float(row.distancia_estimada_km or 0),
        tiempo_min=int(row.tiempo_estimado_min or 0),
        fecha=row.fecha.isoformat(),
    )


@router.get("/portfolios/{cartera_id}/route", response_model=RouteOut)
async def get_route(
    cartera_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras"))],
) -> RouteOut:
    """Clientes del día (mora desc) y el último plan de trayecto guardado."""
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        clientes = await _clientes_del_dia(session, tid, cartera.id)
        plan = await _ultimo_plan(session, tid, cartera.id)
    return RouteOut(clientes=clientes, plan=plan)


@router.post("/portfolios/{cartera_id}/route/optimize", response_model=RoutePlan, status_code=201)
async def optimize_route(
    cartera_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras", write=True))],
) -> RoutePlan:
    """Optimiza el trayecto (vecino más cercano), guarda el plan y lo audita."""
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await _get_active(session, Cartera, cartera_id, tid)
        if cartera is None:
            raise ApiError.not_found("Cartera no encontrada.")
        clientes = await _clientes_del_dia(session, tid, cartera.id)
        apertura = (
            await session.execute(
                select(CarteraApertura)
                .where(
                    CarteraApertura.tenant_id == tid, CarteraApertura.cartera_id == cartera.id
                )
                .order_by(CarteraApertura.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        inicio = (
            (float(apertura.lat), float(apertura.lng))
            if apertura is not None and apertura.lat is not None and apertura.lng is not None
            else None
        )
        orden, km = _ordenar_trayecto(clientes, inicio)
        if not orden:
            raise ApiError.bad_request(
                "Sin clientes con ubicación para optimizar.", "no_located_clients"
            )
        tiempo = int(km / 20 * 60) + len(orden) * 5
        hoy = date.today()
        session.add(
            CarteraOptimizadaLog(
                tenant_id=tid,
                cartera_id=cartera.id,
                fecha=hoy,
                orden_clientes=[str(c) for c in orden],
                distancia_estimada_km=Decimal(str(round(km, 2))),
                tiempo_estimado_min=tiempo,
                tipo="planificada",
            )
        )
        _audit(
            session,
            tid,
            "trayecto",
            cartera.id,
            "crear",
            despues={"clientes": len(orden), "km": round(km, 2)},
        )
        return RoutePlan(
            orden=orden,
            distancia_km=round(km, 2),
            tiempo_min=tiempo,
            fecha=hoy.isoformat(),
        )


# ------------------------------------- cambios de cliente (cierre 3.5)
class ClientChange(BaseModel):
    id: uuid.UUID
    campos_propuestos: dict[str, Any]
    estado: str
    solicitado_por: str
    fecha: str
    motivo_rechazo: str | None = None


def _as_change(row: CambioCliente) -> ClientChange:
    return ClientChange(
        id=row.id,
        campos_propuestos=row.campos_propuestos or {},
        estado=row.estado,
        solicitado_por=row.solicitado_por_rol or "",
        fecha=row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
        motivo_rechazo=row.motivo_rechazo,
    )


class ClientChangeDecision(BaseModel):
    decision: str
    motivo: str = ""


CHANGE_FIELDS = (
    "nombre",
    "apellido",
    "negocio",
    "telefono_whatsapp",
    "tipo_documento",
    "numero_documento",
)


@router.get("/client-changes", response_model=list[ClientChange])
async def list_client_changes(
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes"))],
    cliente_id: uuid.UUID | None = None,
    estado: str = "pendiente",
) -> list[ClientChange]:
    tid = principal.tenant_id
    query = select(CambioCliente).where(CambioCliente.tenant_id == tid)
    if cliente_id is not None:
        query = query.where(CambioCliente.cliente_id == cliente_id)
    if estado:
        query = query.where(CambioCliente.estado == estado)
    async with get_session() as session:
        rows = (
            (await session.execute(query.order_by(CambioCliente.created_at.desc()).limit(200)))
            .scalars()
            .all()
        )
    return [_as_change(r) for r in rows]


@router.post("/client-changes/{change_id}/decision", response_model=ClientChange)
async def decide_client_change(
    change_id: uuid.UUID,
    payload: ClientChangeDecision,
    principal: Annotated[Principal, Depends(require("pack_cobranza:clientes", write=True))],
) -> ClientChange:
    """Aprueba (aplica los campos propuestos al cliente) o rechaza (con motivo)."""
    if payload.decision not in ("aprobar", "rechazar"):
        raise ApiError.bad_request("Decisión inválida: usa 'aprobar' o 'rechazar'.", "invalid_decision")
    tid = principal.tenant_id
    async with get_session() as session:
        cambio = (
            await session.execute(
                select(CambioCliente).where(
                    CambioCliente.id == change_id, CambioCliente.tenant_id == tid
                )
            )
        ).scalar_one_or_none()
        if cambio is None:
            raise ApiError.not_found("Cambio no encontrado.")
        if cambio.estado != "pendiente":
            raise ApiError.bad_request("El cambio ya fue revisado.", "already_reviewed")
        aplicados: dict[str, Any] = {}
        if payload.decision == "aprobar":
            cliente = await _get_active(session, Cliente, cambio.cliente_id, tid)
            if cliente is None:
                raise ApiError.not_found("Cliente no encontrado.")
            for campo, valor in (cambio.campos_propuestos or {}).items():
                if campo in CHANGE_FIELDS and valor is not None:
                    setattr(cliente, campo, str(valor))
                    aplicados[campo] = str(valor)
            cambio.estado = "aprobado"
        else:
            cambio.estado = "rechazado"
            cambio.motivo_rechazo = payload.motivo.strip() or None
        cambio.revisado_por = principal.admin.id
        cambio.revisado_en = datetime.now(UTC)
        _audit(
            session,
            tid,
            "cambio_cliente",
            cambio.id,
            "editar",
            antes={"estado": "pendiente"},
            despues={"estado": cambio.estado, **aplicados},
            motivo=cambio.motivo_rechazo,
        )
        return _as_change(cambio)


class GestorTokenRequest(BaseModel):
    """Credenciales de dispositivo opcionales para el enforcement (ADR-0068)."""

    imei: str | None = None
    whatsapp: str | None = None


@router.post("/gestor-token")
async def gestor_token(
    payload: GestorTokenRequest,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> JSONResponse:
    """Sesión admin → par de tokens de campo del negocio activo (HU-APP-02, ADR-0098).

    Reusa la lógica del login unificado de la API de campo (ADR-0081): resuelve
    la membresía de campo del admin en el negocio activo (`X-Tenant-Id`) y emite
    access/refresh con el enforcement de vinculación de dispositivo. El core no
    conoce a los usuarios de campo; este endpoint vive en el pack.
    """
    from .api_gestor import _membresias_de_campo, _sesion_de_membresia

    if principal.tenant_id is None:
        raise ApiError(400, "sin_negocio", "Selecciona un negocio (X-Tenant-Id).")
    membresias = await _membresias_de_campo(principal.admin, solo=principal.tenant_id)
    if not membresias:
        raise ApiError(403, "sin_acceso_campo", "Sin acceso de campo en este negocio.")
    return await _sesion_de_membresia(
        request,
        principal.admin,
        membresias[0],
        payload.imei or "",
        payload.whatsapp or "",
    )
