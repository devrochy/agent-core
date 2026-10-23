"""API admin de pack-subscriptions (ADR-0079).

Montada en `/api/v1/pack/pack_subscriptions`. Permite al admin crear una
suscripción manualmente a partir de un snapshot del plan (nombre, precio,
moneda, periodicidad, trial) — mismo formato que commerce entrega al activar
por pago. Idempotente por cliente: si ya hay una activa, devuelve la existente.

SPEC-35 (HU-SUB-08/19/20/16): lectura de suscripciones, cargos (los «pagos»), clientes y
métricas, y las acciones pausar / reanudar / cancelar / reenviar link con auditoría del
usuario. Las consultas viven en `consultas.py`; las operaciones son las del proveedor del
contrato (`SubscriptionCapabilityImpl`), que no se reescriben acá.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.pagination import Page, PageParams, build_page, page_params
from pack_subscriptions import consultas
from pack_subscriptions.esquemas import (
    AccionOut,
    CargoItem,
    ClienteConSuscripcion,
    Metricas,
    MotivoBody,
    ReenvioOut,
    SuscripcionDetalle,
    SuscripcionItem,
)

PLUGIN_ID = "pack_subscriptions"
router = APIRouter(prefix="/api/v1/pack/pack_subscriptions", tags=["pack · suscripciones"])


class CrearSuscripcionBody(BaseModel):
    customer_id: uuid.UUID = Field(..., description="Customer del core (pivote multi-pack).")
    variant_id: uuid.UUID | None = Field(default=None, description="Variante de commerce (si aplica).")
    nombre: str = Field(..., description="Nombre del plan.")
    precio: float = Field(default=0, ge=0, le=999_999_999_999, allow_inf_nan=False, description="Valor del período.")
    moneda: str = Field(default="COP")
    periodicidad: str = Field(default="mensual")
    trial_dias: int = Field(default=0, ge=0, le=365, description="Días de prueba (0 = sin prueba).")


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


# ----------------------------------------------------------------------------- lectura (SPEC-35)
SECCION = "pack_subscriptions:suscripciones"
EstadoSub = Literal["trialing", "activa", "pausada", "vencida", "cancelada"]
EstadoCargo = Literal["pendiente", "pagado", "anulado"]
TipoCargo = Literal["periodo", "cambio_plan"]
Periodicidad = Literal["mensual", "trimestral", "anual"]


@router.get("/suscripciones", response_model=Page[SuscripcionItem])
async def listar_suscripciones(
    principal: Annotated[Principal, Depends(require(SECCION))],
    params: Annotated[PageParams, Depends(page_params)],
    estado: Annotated[EstadoSub | None, Query()] = None,
    variante_id: Annotated[uuid.UUID | None, Query(description="Plan (variante de commerce).")] = None,
    periodicidad: Annotated[Periodicidad | None, Query()] = None,
    con_mora: Annotated[bool, Query(description="Solo con algún cargo vencido sin pagar.")] = False,
    q: Annotated[str | None, Query(max_length=100, description="Nombre o teléfono del cliente, o plan.")] = None,
    cliente_id: Annotated[uuid.UUID | None, Query(description="Solo las de este cliente.")] = None,
) -> Page[SuscripcionItem]:
    """Suscripciones del negocio con cliente, plan, estado, próximo cargo y deuda (HU-SUB-08)."""
    from core.db.session import get_session

    async with get_session() as session:
        items, total = await consultas.listar_suscripciones(
            session,
            _tenant(principal),
            params,
            estado=estado,
            variante_id=variante_id,
            periodicidad=periodicidad,
            con_mora=con_mora,
            q=q,
            cliente_id=cliente_id,
        )
    return build_page(items, total, params)


@router.get("/suscripciones/{subscription_id}", response_model=SuscripcionDetalle)
async def detalle_suscripcion(
    subscription_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require(SECCION))],
) -> SuscripcionDetalle:
    """Detalle: cargos, cambio programado, historial de cambios de estado y acciones válidas."""
    from core.db.session import get_session

    async with get_session() as session:
        detalle = await consultas.detalle_suscripcion(session, _tenant(principal), subscription_id)
    if detalle is None:
        raise ApiError.not_found("Suscripción no encontrada.")
    return detalle


@router.get("/cargos", response_model=Page[CargoItem])
async def listar_cargos(
    principal: Annotated[Principal, Depends(require(SECCION))],
    params: Annotated[PageParams, Depends(page_params)],
    estado: Annotated[EstadoCargo | None, Query()] = None,
    tipo: Annotated[TipoCargo | None, Query()] = None,
    suscripcion_id: Annotated[uuid.UUID | None, Query()] = None,
    desde: Annotated[date | None, Query(description="Vencimiento desde (incluido).")] = None,
    hasta: Annotated[date | None, Query(description="Vencimiento hasta (incluido).")] = None,
    vencidos: Annotated[bool, Query(description="Solo pendientes con vencimiento cumplido.")] = False,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[CargoItem]:
    """Pagos de suscripción: los cargos con su estado, vencimiento y mora (HU-SUB-20)."""
    from core.db.session import get_session

    async with get_session() as session:
        items, total = await consultas.listar_cargos(
            session,
            _tenant(principal),
            params,
            estado=estado,
            tipo=tipo,
            suscripcion_id=suscripcion_id,
            desde=desde,
            hasta=hasta,
            vencidos=vencidos,
            q=q,
        )
    return build_page(items, total, params)


@router.get("/clientes", response_model=Page[ClienteConSuscripcion])
async def listar_clientes(
    principal: Annotated[Principal, Depends(require(SECCION))],
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=100)] = None,
    con_mora: Annotated[bool, Query()] = False,
    incluir_canceladas: Annotated[bool, Query(description="Incluye a quien solo tiene suscripciones canceladas.")] = False,
) -> Page[ClienteConSuscripcion]:
    """Clientes con suscripción: cuántas, qué planes, lo que deben y su último período pagado (HU-SUB-19)."""
    from core.db.session import get_session

    async with get_session() as session:
        items, total = await consultas.listar_clientes(
            session,
            _tenant(principal),
            params,
            q=q,
            con_mora=con_mora,
            incluir_canceladas=incluir_canceladas,
        )
    return build_page(items, total, params)


@router.get("/metricas", response_model=Metricas)
async def metricas(
    principal: Annotated[Principal, Depends(require(SECCION))],
) -> Metricas:
    """MRR, altas, bajas, churn, mora por antigüedad y cohortes por plan (HU-SUB-16)."""
    from core.db.session import get_session

    async with get_session() as session:
        return await consultas.metricas(session, _tenant(principal))


# ----------------------------------------------------------------------------- acciones (SPEC-35)
_ACCION_NO_VALIDA = {
    "pausar": "Solo se puede pausar una suscripción en prueba, activa o vencida.",
    "reanudar": "Solo se puede reanudar una suscripción pausada.",
    "cancelar": "Esta suscripción ya está cancelada.",
}


def _tenant(principal: Principal) -> uuid.UUID:
    """Negocio activo del request; una cuenta sin negocio operativo no ve ni actúa sobre nada."""
    if principal.tenant_id is None:
        raise ApiError.not_found("No hay un negocio activo.")
    return principal.tenant_id


def _actor(principal: Principal) -> str:
    return f"admin:{principal.admin.email}"


async def _suscripcion_del_negocio(subscription_id: uuid.UUID, principal: Principal):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from core.db.session import get_session
    from pack_subscriptions.models import Subscription

    async with get_session() as session:
        sub = (
            await session.execute(
                select(Subscription).where(
                    Subscription.tenant_id == principal.tenant_id,
                    Subscription.id == subscription_id,
                    Subscription.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if sub is None:
        raise ApiError.not_found("Suscripción no encontrada.")
    return sub


async def _cambiar_estado(
    accion: Literal["pausar", "reanudar", "cancelar"],
    subscription_id: uuid.UUID,
    principal: Principal,
    motivo: str = "",
) -> AccionOut:
    from core.subscriptions import SubscriptionError
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    sub = await _suscripcion_del_negocio(subscription_id, principal)
    if accion not in consultas.acciones_validas(sub.estado):
        raise ApiError(409, "transicion_invalida", _ACCION_NO_VALIDA[accion])
    cap = SubscriptionCapabilityImpl()
    try:
        if accion == "pausar":
            vista = await cap.pause(subscription_id, reason=motivo, actor=_actor(principal))
        elif accion == "reanudar":
            vista = await cap.resume(subscription_id, actor=_actor(principal))
        else:
            vista = await cap.cancel(subscription_id, reason=motivo, actor=_actor(principal))
    except SubscriptionError as exc:
        raise ApiError(409, exc.code or "error_suscripcion", str(exc)) from exc
    return AccionOut(
        id=subscription_id,
        estado=vista["estado"],
        motivo_pausa="cliente" if vista["estado"] == "pausada" else None,
        proximo_cargo=vista["proximo_cargo"],
    )


@router.post("/suscripciones/{subscription_id}/pausar", response_model=AccionOut)
async def pausar_suscripcion(
    subscription_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require(SECCION, write=True))],
    body: MotivoBody | None = None,
) -> AccionOut:
    """Suspende los cobros (pausa pedida por el negocio: `motivo_pausa = cliente`). Auditada."""
    return await _cambiar_estado("pausar", subscription_id, principal, body.motivo if body else "")


@router.post("/suscripciones/{subscription_id}/reanudar", response_model=AccionOut)
async def reanudar_suscripcion(
    subscription_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require(SECCION, write=True))],
) -> AccionOut:
    """Vuelve a `activa` y calcula el próximo cargo desde hoy. Auditada."""
    return await _cambiar_estado("reanudar", subscription_id, principal)


@router.post("/suscripciones/{subscription_id}/cancelar", response_model=AccionOut)
async def cancelar_suscripcion(
    subscription_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require(SECCION, write=True))],
    body: MotivoBody | None = None,
) -> AccionOut:
    """Termina la suscripción (no se puede reanudar) y deja de generar cargos. Auditada."""
    return await _cambiar_estado("cancelar", subscription_id, principal, body.motivo if body else "")


@router.post("/cargos/{charge_id}/reenviar-link", response_model=ReenvioOut)
async def reenviar_link_de_cargo(
    charge_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require(SECCION, write=True))],
) -> ReenvioOut:
    """Reenvía al cliente el link de pago de un cargo pendiente, por WhatsApp (outbox, ADR-0107).

    Reusa el link del cargo (o lo crea si falta, sin duplicar el cargo). Si el cliente no tiene
    una conversación de WhatsApp no se envía nada y se devuelve el link para copiarlo.
    """
    from sqlalchemy import select

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.events.core_handlers import _whatsapp_conversation_for
    from core.events.outbox import emit_domain
    from core.shared.events import ON_SUBSCRIPTION_CHARGE_REMINDER
    from core.subscriptions import SubscriptionError
    from pack_subscriptions.capability import SubscriptionCapabilityImpl, _config
    from pack_subscriptions.jobs import _payload_cargo
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    async def cargar() -> tuple[SubscriptionCharge, Subscription]:
        async with get_session() as session:
            fila = (
                await session.execute(
                    select(SubscriptionCharge, Subscription)
                    .join(Subscription, Subscription.id == SubscriptionCharge.subscription_id)
                    .where(
                        SubscriptionCharge.tenant_id == principal.tenant_id,
                        Subscription.tenant_id == principal.tenant_id,
                        SubscriptionCharge.id == charge_id,
                        SubscriptionCharge.deleted_at.is_(None),
                        Subscription.deleted_at.is_(None),
                    )
                )
            ).first()
        if fila is None:
            raise ApiError.not_found("Cargo no encontrado.")
        return fila[0], fila[1]

    cargo, sub = await cargar()
    if cargo.estado != "pendiente" or cargo.tipo != "periodo":
        raise ApiError(409, "cargo_no_pendiente", "Solo se reenvía el link de un cobro de período pendiente.")
    if sub.estado == "cancelada":
        raise ApiError(409, "suscripcion_cancelada", "La suscripción está cancelada: no hay nada que cobrar.")
    if not cargo.link_url:
        try:
            await SubscriptionCapabilityImpl().charge_link(sub.id, period=cargo.periodo)
        except SubscriptionError as exc:
            raise ApiError(409, exc.code or "error_suscripcion", str(exc)) from exc
        cargo, sub = await cargar()
    if not cargo.link_url:
        raise ApiError(409, "sin_link", "El cobro todavía no tiene un link de pago.")

    conversacion = await _whatsapp_conversation_for(sub.customer_id)
    ahora = datetime.now(UTC)
    enviado = conversacion is not None
    async with get_session() as session:
        if enviado:
            config = await _config()
            vence = cargo.fecha_vencimiento or ahora
            sello = ahora.strftime("%Y%m%d%H%M")  # un clic repetido en el mismo minuto no duplica el aviso
            await emit_domain(
                session,
                tenant_id=_tenant(principal),
                topic=ON_SUBSCRIPTION_CHARGE_REMINDER,
                dedupe_key=f"sub_resend:{cargo.id}:{sello}",
                payload={
                    **_payload_cargo(cargo, sub),
                    "dias": (ahora.date() - vence.date()).days,
                    "texto": config.recordatorio_texto,
                    "reenvio": sello,
                },
            )
        session.add(
            AuditLog(
                tenant_id=_tenant(principal),
                actor=_actor(principal),
                action="subs_link_reenviado",
                input={"subscription_id": str(sub.id), "charge_id": str(cargo.id), "periodo": cargo.periodo},
                output={"enviado": enviado},
            )
        )
    return ReenvioOut(
        enviado=enviado,
        link=cargo.link_url,
        motivo=None if enviado else "sin_conversacion_whatsapp",
    )
