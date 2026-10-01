"""API admin de pack-subscriptions (ADR-0079).

Montada en `/api/v1/pack/pack_subscriptions`. Permite al admin crear una
suscripción manualmente a partir de un snapshot del plan (nombre, precio,
moneda, periodicidad, trial) — mismo formato que commerce entrega al activar
por pago. Idempotente por cliente: si ya hay una activa, devuelve la existente.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.api.deps import Principal, require

PLUGIN_ID = "pack_subscriptions"
router = APIRouter(prefix="/api/v1/pack/pack_subscriptions", tags=["pack · suscripciones"])


class CrearSuscripcionBody(BaseModel):
    customer_id: uuid.UUID = Field(..., description="Customer del core (pivote multi-pack).")
    variant_id: uuid.UUID | None = Field(default=None, description="Variante de commerce (si aplica).")
    nombre: str = Field(..., description="Nombre del plan.")
    precio: float = Field(default=0, description="Valor del período.")
    moneda: str = Field(default="COP")
    periodicidad: str = Field(default="mensual")
    trial_dias: int = Field(default=0, description="Días de prueba (0 = sin prueba).")


class SuscripcionOut(BaseModel):
    creada: bool
    suscripcion_id: uuid.UUID
    plan: str
    estado: str


@router.post("/suscripciones", response_model=SuscripcionOut)
async def crear_suscripcion_manual(
    body: CrearSuscripcionBody,
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones"))],
) -> SuscripcionOut:
    """Crea (o reutiliza) la suscripción de un `Customer` a un plan."""
    from pack_subscriptions.module import (
        crear_suscripcion_para,  # import perezoso: evita ciclo module↔admin_api
    )

    plan = {
        "nombre": body.nombre,
        "precio": body.precio,
        "moneda": body.moneda,
        "periodicidad": body.periodicidad,
        "trial_dias": body.trial_dias,
    }
    sub = await crear_suscripcion_para(
        customer_id=body.customer_id,
        variante_id=body.variant_id,
        plan=plan,
        reference="admin-manual",
    )
    return SuscripcionOut(
        creada=sub.estado in ("trialing", "activa"),
        suscripcion_id=sub.id,
        plan=sub.concepto,
        estado=sub.estado,
    )