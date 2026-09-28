"""API admin del pack de Cobranza (ADR-0079) — núcleo.

Montada en `/api/v1/pack/cobranza`. Cubre lo más usado del panel: resumen,
reglas de negociación, carteras, cobros (pagos), promesas y gestores. El resto
(carteras a fondo, monitoreo, dispositivos, alertas, auditoría, reportes,
propietario) sigue en el panel clásico enlazado.
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
from core.db.session import get_owned, get_session

from .models import (
    Cartera,
    Cliente,
    Cuota,
    Gasto,
    Gestor,
    Pago,
    Prestamo,
    Promesa,
    ReglaNegociacion,
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
