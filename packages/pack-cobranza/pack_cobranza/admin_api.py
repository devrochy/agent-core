"""API admin del pack de Cobranza (ADR-0079).

Montada en `/api/v1/pack/cobranza`. Cubre lo más usado del panel: resumen,
reglas de negociación, carteras, cobros (pagos), promesas, gestores y las
secciones operativas (monitoreo, alertas de acceso y auditoría). El resto
(carteras a fondo, dispositivos, reportes, propietario, caja/liquidaciones)
sigue en el panel clásico enlazado.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import func, select

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.db.models import Conversation, Escalation
from core.db.session import get_owned, get_session

from .models import (
    AuditoriaCartera,
    Caja,
    Cartera,
    Cliente,
    CobroPropietario,
    ConversacionPropietario,
    Cuota,
    Device,
    Gasto,
    Gestor,
    IntentoAcceso,
    Inyeccion,
    LinkPago,
    Liquidacion,
    MensajePropietario,
    Pago,
    Prestamo,
    Promesa,
    ReglaNegociacion,
    SyncEvento,
)

PLUGIN_ID = "pack_cobranza"
router = APIRouter(prefix="/api/v1/pack/cobranza", tags=["pack · cobranza"])

PROMISE_STATUSES = ("pendiente", "cumplida", "incumplida")


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
class Rules(BaseModel):
    max_dias_prorroga: int
    min_abono_pct: float
    max_reprogramaciones: int
    umbral_saldo_autonomo: float


class RulesUpdate(BaseModel):
    max_dias_prorroga: int = 15
    min_abono_pct: float = 10.0
    max_reprogramaciones: int = 2
    umbral_saldo_autonomo: float = 0.0


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
    )


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
    return await get_rules(principal)


# ------------------------------------------------------------------ carteras
class Portfolio(BaseModel):
    id: uuid.UUID
    nombre: str
    descripcion: str | None
    gestor: str | None
    tipo_interes: float
    num_cuotas: int
    moneda: str
    estatus: str


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
        Portfolio(
            id=c.id,
            nombre=c.nombre,
            descripcion=c.descripcion,
            gestor=name_by_id.get(c.gestor_id) if c.gestor_id else None,
            tipo_interes=float(c.tipo_interes or 0),
            num_cuotas=int(c.num_cuotas or 1),
            moneda=c.moneda or "COP",
            estatus=c.estatus,
        )
        for c in rows
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
    name_by_id = {c.id: f"{c.nombres} {c.apellidos or ''}".strip() for c in clientes}
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
    name_by_id = {c.id: f"{c.nombres} {c.apellidos or ''}".strip() for c in clientes}
    return [
        Promise(
            id=p.id,
            cliente=name_by_id.get(p.cliente_id, "Cliente"),
            fecha_prometida=p.fecha_prometida,
            valor_prometido=float(p.valor_prometido or 0),
            estado=p.estado,
            tipo=p.tipo,
            creado_por=p.creado_por,
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
        row = await get_owned(session, Promesa, promise_id, principal.tenant_id)
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
    return {c.id: f"{c.nombres} {c.apellidos or ''}".strip() for c in rows}


def _as_promise(row: Promesa, cliente: str) -> Promise:
    return Promise(
        id=row.id,
        cliente=cliente,
        fecha_prometida=row.fecha_prometida,
        valor_prometido=float(row.valor_prometido or 0),
        estado=row.estado,
        tipo=row.tipo,
        creado_por=row.creado_por,
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
    despues: dict[str, Any] | None = None,
    motivo: str | None = None,
) -> None:
    session.add(
        AuditoriaCartera(
            tenant_id=tid,
            entidad=entidad,
            entidad_id=entidad_id,
            operacion=operacion,
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
        device = await get_owned(session, Device, device_id, principal.tenant_id)
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
        cobro = await get_owned(session, CobroPropietario, charge_id, principal.tenant_id)
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
    fecha: str
    descripcion: str
    valor: float
    aprobado: bool


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
    caja: PortfolioCaja
    liquidaciones: list[Liquidation]
    inyecciones: list[Injection]
    gastos: list[Expense]


@router.get("/portfolios/{cartera_id}", response_model=PortfolioDetail)
async def portfolio_detail(
    cartera_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_cobranza:carteras"))],
) -> PortfolioDetail:
    tid = principal.tenant_id
    async with get_session() as session:
        cartera = await get_owned(session, Cartera, cartera_id, tid)
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
                fecha=g.fecha_hora.strftime("%Y-%m-%d") if g.fecha_hora else "",
                descripcion=g.descripcion,
                valor=float(g.valor or 0),
                aprobado=bool(g.aprobado),
            )
            for g in gastos
        ],
    )
