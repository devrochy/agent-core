"""Esquemas de la API admin de suscripciones (SPEC-35, HU-SUB-08/19/20/16).

Contratos de lectura que consume el panel. Dinero como `float` (igual que `SubscriptionView`):
los montos son de una moneda por suscripción y el servidor es quien los calcula.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ClienteRef(BaseModel):
    """El `Customer` del core al que pertenece la suscripción (sin email: el core no lo guarda)."""

    id: uuid.UUID
    nombre: str | None = None
    telefono: str | None = None
    canal: str | None = None


class SuscripcionItem(BaseModel):
    id: uuid.UUID
    cliente: ClienteRef
    plan: str
    variante_id: uuid.UUID | None = None
    estado: str  # pendiente_pago | trialing | activa | pausada | vencida | cancelada
    motivo_pausa: str | None = None  # mora | cliente
    periodicidad: str  # mensual | trimestral | anual
    valor_periodo: float
    moneda: str
    proximo_cargo: datetime | None = None
    trial_termina: datetime | None = None
    fecha_alta: datetime | None = None
    deuda: float = Field(description="Suma de los cargos de período pendientes (vencidos o por vencer).")
    cargos_pendientes: int
    en_mora: float = Field(description="Parte de la deuda con vencimiento ya cumplido.")
    dias_mora: int | None = Field(default=None, description="Días del cargo vencido más viejo; None si no hay mora.")


class CargoItem(BaseModel):
    id: uuid.UUID
    suscripcion_id: uuid.UUID
    cliente: ClienteRef | None = None
    plan: str | None = None
    periodo: str
    tipo: str  # periodo | cambio_plan
    monto: float
    moneda: str | None = None
    estado: str  # pendiente | pagado | anulado
    fecha_vencimiento: datetime | None = None
    dias_mora: int | None = None
    link_url: str | None = None
    pago_externo: dict[str, Any] | None = None
    creado: datetime


class HistorialItem(BaseModel):
    at: datetime
    accion: str  # subs_pausada | subs_reanudada | subs_cancelada | subs_pausada_por_mora | subs_link_reenviado
    actor: str
    detalle: str | None = None


class SuscripcionDetalle(SuscripcionItem):
    fecha_cancelacion: datetime | None = None
    cambio_programado: dict[str, Any] | None = None
    cargos: list[CargoItem]
    historial: list[HistorialItem]
    acciones: list[str] = Field(description="Acciones válidas en el estado actual: pausar, reanudar, cancelar.")


class AccionOut(BaseModel):
    id: uuid.UUID
    estado: str
    motivo_pausa: str | None = None
    proximo_cargo: datetime | None = None


class MotivoBody(BaseModel):
    motivo: str = Field(default="", max_length=300, description="Opcional; queda en la auditoría.")


class ReenvioOut(BaseModel):
    enviado: bool
    link: str | None = None
    motivo: str | None = Field(
        default=None,
        description="Por qué no se envió (`sin_conversacion_whatsapp`): el link igual se devuelve para copiarlo.",
    )


class ClienteConSuscripcion(BaseModel):
    cliente: ClienteRef
    suscripciones: int
    vivas: int = Field(description="Suscripciones en prueba, activas, pausadas o vencidas (no canceladas).")
    planes: list[str]
    deuda: float
    en_mora: float
    dias_mora: int | None = None
    ultimo_periodo_pagado: str | None = None


class TramoDeMora(BaseModel):
    etiqueta: str
    cargos: int
    monto: float


class MoraResumen(BaseModel):
    cargos: int
    monto: float
    tramos: list[TramoDeMora]


class CohorteFila(BaseModel):
    cohorte: str  # YYYY-MM del alta
    plan: str
    altas: int
    vigentes: int = Field(description="De esas altas, cuántas no están canceladas hoy.")


class Metricas(BaseModel):
    moneda: str
    otras_monedas: int = Field(description="Suscripciones activas en otra moneda, fuera de estas cifras.")
    mrr: float = Field(description="Ingreso recurrente mensual: activas de pago, trimestral ÷ 3 y anual ÷ 12.")
    mrr_descuento: float = Field(
        default=0,
        description="Descuento vigente por mes (SPEC-43): de las activas de pago con descuento y períodos por consumir, "
        "valor del período por el porcentaje, normalizado a mes. El MRR no lo resta.",
    )
    suscripciones_con_descuento: int = Field(default=0, description="Activas de pago con descuento vigente.")
    mrr_mes_anterior: float = Field(description="Aproximado: suscripciones vivas al cierre del mes anterior, a su precio actual.")
    activas: int
    en_prueba: int
    pendientes_de_pago: int = Field(
        default=0, description="Inscripciones esperando su primer pago: no son suscripciones vivas y no suman a nada."
    )
    pausadas: int
    vencidas: int
    canceladas: int
    altas_mes: int
    altas_mes_anterior: int
    bajas_mes: int
    bajas_mes_anterior: int
    churn_mes: float | None = Field(
        default=None, description="Bajas del mes ÷ suscripciones vivas al inicio del mes (0 a 1)."
    )
    mora: MoraResumen
    cohortes: list[CohorteFila]
    desde: date = Field(description="Primer día del mes en curso (UTC).")


class RegistrarPagoBody(BaseModel):
    referencia: str = Field(
        ...,
        min_length=3,
        max_length=120,
        description="Número del comprobante o texto que identifica la transferencia (para conciliar con el extracto).",
    )


class PagoRegistradoOut(BaseModel):
    estado: Literal["registrado", "ya_registrado"]
    charge_id: uuid.UUID
    periodo: str
    suscripcion_estado: str
    proximo_cargo: datetime | None = None

