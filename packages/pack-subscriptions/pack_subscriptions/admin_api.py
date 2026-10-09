"""API admin de pack-subscriptions (ADR-0079).

Montada en `/api/v1/pack/pack_subscriptions`. Permite al admin crear una
suscripción manualmente a partir de un snapshot del plan (nombre, precio,
moneda, periodicidad, trial) — mismo formato que commerce entrega al activar
por pago. Idempotente por cliente: si ya hay una activa, devuelve la existente.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.api.deps import Principal, require
from core.api.errors import ApiError

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
    # Escritura (HU-SH-04): activar una suscripción con un precio libre es una acción de
    # dinero; un colaborador de solo lectura no puede hacerlo.
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones", write=True))],
) -> SuscripcionOut:
    """Crea (o reutiliza) la suscripción de un `Customer` a un plan."""
    from pack_subscriptions.module import (
        crear_suscripcion_para,  # import perezoso: evita ciclo module↔admin_api
    )

    if body.precio > 0 and body.trial_dias > 0:
        raise HTTPException(
            status_code=422,
            detail="Solo el plan Free ($0) tiene días de prueba (D-16).",
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


class AvisoOut(BaseModel):
    evento: str
    creado: datetime
    estado_evento: str  # pendiente | entregado | fallido (outbox)
    intentos: int
    error_evento: str | None = None
    mensaje_estado: str | None = None  # queued | sent | delivered | read | failed
    mensaje_error: str | None = None  # p. ej. "sin_plantilla"
    plantilla: str | None = None
    enviado: datetime | None = None


_TOPICS_AVISO = (
    "subscription.charge_created",
    "subscription.charge_reminder",
    "subscription.past_due",
    "subscription.paused",
    "subscription.trial_ending",
)


@router.get("/cargos/{charge_id}/avisos", response_model=list[AvisoOut])
async def avisos_del_cargo(
    charge_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones"))],
) -> list[AvisoOut]:
    """Qué se le avisó al cliente por este cargo, cuándo y con qué resultado (HU-SUB-05)."""
    from sqlalchemy import select

    from core.db.models import DomainEvent, Message
    from core.db.session import get_session
    from core.events.core_handlers import notice_key_for
    from pack_subscriptions.models import SubscriptionCharge

    async with get_session() as session:
        cargo = (
            await session.execute(
                select(SubscriptionCharge).where(
                    SubscriptionCharge.id == charge_id,
                    SubscriptionCharge.tenant_id == principal.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if cargo is None:
            raise ApiError.not_found("Cargo no encontrado.")
        eventos = (
            await session.execute(
                select(DomainEvent)
                .where(
                    DomainEvent.tenant_id == principal.tenant_id,
                    DomainEvent.topic.in_(_TOPICS_AVISO),
                    DomainEvent.payload["charge_id"].astext == str(charge_id),
                )
                .order_by(DomainEvent.created_at)
            )
        ).scalars().all()
        avisos: list[AvisoOut] = []
        for ev in eventos:
            clave = notice_key_for(ev.topic, dict(ev.payload or {}))
            mensaje = (
                await session.execute(
                    select(Message)
                    .where(
                        Message.tenant_id == principal.tenant_id,
                        Message.client_message_id.in_((clave, f"{clave}:0")),
                    )
                    .order_by(Message.created_at)
                )
            ).scalars().first()
            avisos.append(
                AvisoOut(
                    evento=ev.topic,
                    creado=ev.created_at,
                    estado_evento=ev.status,
                    intentos=ev.attempts,
                    error_evento=ev.last_error,
                    mensaje_estado=mensaje.status if mensaje else None,
                    mensaje_error=mensaje.error_reason if mensaje else None,
                    plantilla=mensaje.template_name if mensaje else None,
                    enviado=mensaje.created_at if mensaje else None,
                )
            )
    return avisos
