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
    PagoRegistradoOut,
    ReenvioOut,
    RegistrarPagoBody,
    SuscripcionDetalle,
    SuscripcionItem,
)

PLUGIN_ID = "pack_subscriptions"
router = APIRouter(prefix="/api/v1/pack/pack_subscriptions", tags=["pack · suscripciones"])


class CrearSuscripcionBody(BaseModel):
    """Con `plan_codigo` se inscribe al cliente en un plan del catálogo (SPEC-43). Los campos de plan
    libre (`nombre`, `precio`, ...) son el camino anterior, que se retira con commerce sin planes (PR-4)."""

    customer_id: uuid.UUID = Field(..., description="Customer del core (pivote multi-pack).")
    plan_codigo: str | None = Field(default=None, max_length=40, description="Código del plan del catálogo.")
    datos_alta: dict[str, str] | None = Field(
        default=None, description="Datos del cliente para el alta (nombre, email, teléfono, negocio, vertical)."
    )
    metodo_pago: Literal["pago_en_linea", "transferencia"] = Field(default="pago_en_linea")
    codigo_promocion: str | None = Field(
        default=None,
        max_length=40,
        description="Código de una promoción de plan: el primer cobro sale con descuento (HU-SUB-33).",
    )
    variant_id: uuid.UUID | None = Field(default=None, description="Camino anterior: variante de commerce.")
    nombre: str | None = Field(default=None, description="Camino anterior: nombre del plan.")
    precio: float = Field(default=0, ge=0, le=999_999_999_999, allow_inf_nan=False, description="Valor del período.")
    moneda: str = Field(default="COP")
    periodicidad: str = Field(default="mensual")
    trial_dias: int = Field(default=0, ge=0, le=365, description="Días de prueba (0 = sin prueba).")


class SuscripcionOut(BaseModel):
    creada: bool
    suscripcion_id: uuid.UUID
    plan: str
    estado: str
    numero_publico: str | None = None
    inscripcion: str | None = Field(default=None, description="nueva | repetida | cambio_de_plan")
    monto: float | None = Field(default=None, description="Valor del cobro de inscripción (neto), si lo hay.")
    monto_bruto: float | None = Field(default=None, description="Precio de lista, si hay una promoción aplicada.")
    descuento: float | None = Field(default=None, description="Descuento de la promoción en el primer cobro.")
    promocion: str | None = Field(default=None, description="Etiqueta de la promoción aplicada.")
    link_pago: str | None = None
    instrucciones_transferencia: str | None = None


_CODIGOS_404 = {"plan_no_encontrado", "suscripcion_no_encontrada", "cliente_no_encontrado", "plan_no_existe"}
_CODIGOS_422 = {
    "faltan_datos",
    "dato_invalido",
    "origen_invalido",
    "metodo_pago_invalido",
    "referencia_invalida",
    "codigo_no_valido",
    "promo_no_aplica_a_free",
    "promo_no_aplica_a_cambio",
    "promo_no_aplica_a_downgrade",
    "descuento_excede_precio",
}


def _api_error(exc: Exception) -> ApiError:
    """Traduce el rechazo de negocio (`SubscriptionError`/`PlanError`) a un error de API con su código."""
    code = str(getattr(exc, "code", "") or "error_suscripcion")
    status = 404 if code in _CODIGOS_404 else 422 if code in _CODIGOS_422 else 429 if code == "demasiados_intentos" else 409
    campo = getattr(exc, "campo", None)
    return ApiError(
        status,
        code,
        str(getattr(exc, "message", None) or exc),
        fields={campo: str(exc)} if campo else None,
        details=getattr(exc, "data", None) or None,
    )


@router.post("/suscripciones", response_model=SuscripcionOut)
async def crear_suscripcion_manual(
    body: CrearSuscripcionBody,
    # Escritura (HU-SH-04): inscribir o activar una suscripción es una acción de dinero; un
    # colaborador de solo lectura no puede hacerlo.
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones", write=True))],
) -> SuscripcionOut:
    """Inscribe a un `Customer` en un plan del catálogo (o, camino anterior, crea la suscripción de un plan libre).

    Con `plan_codigo`: un plan de $0 nace activa (o en prueba); uno de pago nace `pendiente_pago` con su
    cobro y su link (o las instrucciones de transferencia). Repetir la petición devuelve la misma inscripción.
    """
    if body.plan_codigo:
        return await _inscribir_desde_panel(body, principal)
    if body.codigo_promocion:
        raise ApiError(422, "promo_requiere_plan", "El código de promoción solo aplica al inscribir en un plan (`plan_codigo`).")
    if not body.nombre:
        raise ApiError(422, "plan_requerido", "Indica el plan (`plan_codigo`) en el que se inscribe el cliente.")
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


async def _inscribir_desde_panel(body: CrearSuscripcionBody, principal: Principal) -> SuscripcionOut:
    from sqlalchemy import select

    from core.db.models import Customer
    from core.db.session import get_session
    from core.subscriptions import SubscriptionError
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    tid = _tenant(principal)
    async with get_session() as session:
        existe = (
            await session.execute(
                select(Customer.id).where(
                    Customer.tenant_id == tid, Customer.id == body.customer_id, Customer.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
    if existe is None:
        raise ApiError.not_found("Cliente no encontrado.")
    try:
        vista = await SubscriptionCapabilityImpl().inscribir(
            customer_id=body.customer_id,
            plan_code=body.plan_codigo or "",
            datos_alta=dict(body.datos_alta or {}),
            origen="panel",
            metodo_pago=body.metodo_pago,
            codigo_promocion=body.codigo_promocion,
        )
    except SubscriptionError as exc:
        raise _api_error(exc) from exc
    cargo = vista.get("cargo_inscripcion") or {}
    return SuscripcionOut(
        creada=vista["estado"] in ("trialing", "activa"),
        suscripcion_id=uuid.UUID(str(vista["subscription_id"])),
        plan=str(vista.get("plan") or ""),
        estado=vista["estado"],
        numero_publico=vista.get("numero_publico"),
        inscripcion=vista.get("inscripcion"),
        monto=cargo.get("monto"),
        monto_bruto=cargo.get("monto_bruto"),
        descuento=cargo.get("descuento"),
        promocion=cargo.get("promocion"),
        link_pago=cargo.get("link"),
        instrucciones_transferencia=cargo.get("instrucciones_transferencia"),
    )


class ConfirmarPagoBody(BaseModel):
    referencia: str = Field(min_length=3, max_length=120, description="Número del comprobante o referencia del pago.")


class PagoConfirmadoOut(BaseModel):
    estado: Literal["confirmado", "ya_confirmado"]
    suscripcion_id: uuid.UUID
    cargo_id: uuid.UUID
    suscripcion_estado: str
    numero_publico: str | None = None


@router.post("/suscripciones/{subscription_id}/confirmar-pago", response_model=PagoConfirmadoOut)
async def confirmar_pago_de_inscripcion(
    subscription_id: uuid.UUID,
    body: ConfirmarPagoBody,
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones", write=True))],
) -> PagoConfirmadoOut:
    """Confirma a mano el pago por transferencia de una inscripción `pendiente_pago` (o de un cambio de plan).

    Activa por el mismo camino que el webhook del pago en línea; repetir con la misma referencia responde
    `ya_confirmado`. Queda auditado con el usuario."""
    from core.subscriptions import SubscriptionError
    from pack_subscriptions import inscripcion

    await _suscripcion_del_negocio(subscription_id, principal)
    try:
        r = await inscripcion.confirmar_pago(subscription_id, referencia=body.referencia, actor=_actor(principal))
    except SubscriptionError as exc:
        raise _api_error(exc) from exc
    return PagoConfirmadoOut(
        estado=r["estado"],
        suscripcion_id=uuid.UUID(r["subscription_id"]),
        cargo_id=uuid.UUID(r["charge_id"]),
        suscripcion_estado=r["suscripcion_estado"],
        numero_publico=r.get("numero_publico"),
    )


# ----------------------------------------------------------------------------- catálogo de planes (SPEC-43)
class PlanBody(BaseModel):
    codigo: str = Field(min_length=1, max_length=40, description="SKU del plan, único por negocio.")
    nombre: str = Field(min_length=1, max_length=120)
    descripcion: str = Field(default="", max_length=2000)
    precio: float = Field(ge=0, le=999_999_999_999, allow_inf_nan=False)
    moneda: str = Field(default="COP", min_length=3, max_length=3)
    periodicidad: Literal["mensual", "trimestral", "anual"] = "mensual"
    trial_dias: int = Field(default=0, ge=0, le=365, description="Solo un plan de $0 tiene prueba (D-16).")
    entitlements: dict[str, object] = Field(default_factory=dict, description="Lo que incluye el plan (objeto).")
    plan_plataforma: str | None = Field(default=None, max_length=40, description="Plan de la plataforma que vende.")
    activo: bool = True
    orden: int = Field(default=0, ge=-10_000, le=10_000)


class PlanOut(PlanBody):
    id: uuid.UUID
    creado: datetime | None = None
    actualizado: datetime | None = None


def _plan_out(plan) -> PlanOut:  # type: ignore[no-untyped-def]
    return PlanOut(
        id=plan.id,
        codigo=plan.codigo,
        nombre=plan.nombre,
        descripcion=plan.descripcion or "",
        precio=float(plan.precio or 0),
        moneda=plan.moneda,
        periodicidad=plan.periodicidad,
        trial_dias=int(plan.trial_dias or 0),
        entitlements=dict(plan.entitlements or {}),
        plan_plataforma=plan.plan_plataforma,
        activo=bool(plan.activo),
        orden=int(plan.orden or 0),
        creado=plan.created_at,
        actualizado=plan.updated_at,
    )


def _datos_de_plan(body: PlanBody):  # type: ignore[no-untyped-def]
    from decimal import Decimal

    from pack_subscriptions.planes import DatosDePlan

    return DatosDePlan(
        codigo=body.codigo,
        nombre=body.nombre,
        descripcion=body.descripcion,
        precio=Decimal(str(body.precio)),
        moneda=body.moneda,
        periodicidad=body.periodicidad,
        trial_dias=body.trial_dias,
        entitlements=dict(body.entitlements),
        plan_plataforma=body.plan_plataforma,
        activo=body.activo,
        orden=body.orden,
    )


@router.get("/planes", response_model=list[PlanOut])
async def listar_planes(
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones"))],
    incluir_inactivos: Annotated[bool, Query(description="Incluye los planes apagados.")] = True,
) -> list[PlanOut]:
    """Catálogo de planes del negocio (sin los borrados), en el orden en que se ofrecen."""
    from pack_subscriptions import planes

    _tenant(principal)
    return [_plan_out(p) for p in await planes.listar(incluir_inactivos=incluir_inactivos)]


@router.post("/planes", response_model=PlanOut, status_code=201)
async def crear_plan(
    body: PlanBody,
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones", write=True))],
) -> PlanOut:
    """Crea un plan. Código repetido → 409 `codigo_duplicado`; prueba en un plan de pago → 422."""
    from pack_subscriptions import planes

    _tenant(principal)
    try:
        return _plan_out(await planes.crear(_datos_de_plan(body), actor=_actor(principal)))
    except planes.PlanError as exc:
        raise _api_error_plan(exc) from exc


@router.put("/planes/{plan_id}", response_model=PlanOut)
async def editar_plan(
    plan_id: uuid.UUID,
    body: PlanBody,
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones", write=True))],
) -> PlanOut:
    """Reemplaza un plan. Las suscripciones ya inscritas conservan su snapshot (precio y nombre de cuando entraron)."""
    from pack_subscriptions import planes

    _tenant(principal)
    try:
        return _plan_out(await planes.actualizar(plan_id, _datos_de_plan(body), actor=_actor(principal)))
    except planes.PlanError as exc:
        raise _api_error_plan(exc) from exc


@router.delete("/planes/{plan_id}", status_code=204)
async def borrar_plan(
    plan_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones", write=True))],
) -> None:
    """Borra el plan (lógico): deja de ofrecerse; las suscripciones vivas siguen cobrándose."""
    from pack_subscriptions import planes

    _tenant(principal)
    try:
        await planes.borrar(plan_id, actor=_actor(principal))
    except planes.PlanError as exc:
        raise _api_error_plan(exc) from exc


def _api_error_plan(exc) -> ApiError:  # type: ignore[no-untyped-def]
    status = 404 if exc.code == "plan_no_encontrado" else 409 if exc.code == "codigo_duplicado" else 422
    return ApiError(status, exc.code, exc.message, fields={exc.campo: exc.message} if exc.campo else None)


# ----------------------------------------------------------------------------- promociones de plan (SPEC-43, HU-SUB-33)
class PromocionBody(BaseModel):
    etiqueta: str = Field(min_length=1, max_length=80, description="Nombre que ve el dueño (p. ej. «Lanzamiento»).")
    codigo: str | None = Field(
        default=None,
        max_length=40,
        description="Código que escribe el cliente (3 a 40: letras, números, - o _). Se guarda en minúsculas.",
    )
    tipo_descuento: Literal["porcentaje", "monto"] = "porcentaje"
    valor: float = Field(gt=0, le=999_999_999_999, allow_inf_nan=False, description="1 a 99 si es porcentaje.")
    periodos: int = Field(default=1, ge=1, le=12, description="Cobros que descuenta. De 1 a 12 períodos; el monto fijo solo descuenta 1.")
    plan_id: uuid.UUID | None = Field(default=None, description="Plan al que aplica; vacío = cualquier plan de pago.")
    vigente_desde: datetime | None = None
    vigente_hasta: datetime | None = None
    max_canjes: int | None = Field(default=None, ge=1, le=1_000_000, description="Tope de canjes; vacío = sin tope.")
    activa: bool = True


class PromocionOut(PromocionBody):
    id: uuid.UUID
    canjes_usados: int
    canjes_restantes: int | None = Field(default=None, description="Vacío si no tiene tope.")
    creada: datetime | None = None
    actualizada: datetime | None = None


class CanjeOut(BaseModel):
    id: uuid.UUID
    estado: Literal["reservado", "confirmado", "agotado", "terminado", "liberado"]
    cliente_id: uuid.UUID
    suscripcion_id: uuid.UUID
    cargo_id: uuid.UUID
    monto_bruto: float | None = None
    descuento: float | None = None
    monto: float | None = Field(default=None, description="Neto cobrado.")
    creado: datetime | None = None


def _enmascarar_codigo(codigo: str | None) -> str | None:
    """Versión parcial del código del cupón para quien solo puede leer (`lan•••••`)."""
    if not codigo:
        return None
    return codigo[:3] + "•" * max(len(codigo) - 3, 3)


def _promocion_out(promo, *, ver_codigo: bool = True) -> PromocionOut:  # type: ignore[no-untyped-def]
    restantes = None if promo.max_canjes is None else max(promo.max_canjes - promo.canjes_usados, 0)
    return PromocionOut(
        id=promo.id,
        etiqueta=promo.etiqueta,
        codigo=promo.codigo if ver_codigo else _enmascarar_codigo(promo.codigo),
        tipo_descuento=promo.tipo_descuento,
        valor=float(promo.valor),
        periodos=int(promo.periodos),
        plan_id=promo.plan_id,
        vigente_desde=promo.vigente_desde,
        vigente_hasta=promo.vigente_hasta,
        max_canjes=promo.max_canjes,
        activa=bool(promo.activa),
        canjes_usados=int(promo.canjes_usados),
        canjes_restantes=restantes,
        creada=promo.created_at,
        actualizada=promo.updated_at,
    )


def _datos_de_promocion(body: PromocionBody):  # type: ignore[no-untyped-def]
    from decimal import Decimal

    from pack_subscriptions.promociones import DatosDePromocion

    return DatosDePromocion(
        etiqueta=body.etiqueta,
        tipo_descuento=body.tipo_descuento,
        valor=Decimal(str(body.valor)),
        codigo=body.codigo,
        periodos=body.periodos,
        plan_id=body.plan_id,
        vigente_desde=body.vigente_desde,
        vigente_hasta=body.vigente_hasta,
        max_canjes=body.max_canjes,
        activa=body.activa,
    )


def _api_error_promocion(exc) -> ApiError:  # type: ignore[no-untyped-def]
    status = 404 if exc.code in ("promocion_no_encontrada", "plan_no_encontrado") else 409 if exc.code == "codigo_duplicado" else 422
    return ApiError(status, exc.code, exc.message, fields={exc.campo: exc.message} if exc.campo else None)


@router.get("/promociones", response_model=list[PromocionOut])
async def listar_promociones(
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones"))],
) -> list[PromocionOut]:
    """Promociones de plan del negocio (sin las borradas), de la más reciente a la más antigua."""
    from pack_subscriptions import promociones

    _tenant(principal)
    # El código del cupón es un secreto: solo quien puede escribir lo ve completo.
    ver_codigo = principal.admin.role == "master" or principal.permissions.get("pack_subscriptions:suscripciones") == "write"
    return [_promocion_out(p, ver_codigo=ver_codigo) for p in await promociones.listar()]


@router.post("/promociones", response_model=PromocionOut, status_code=201)
async def crear_promocion(
    body: PromocionBody,
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones", write=True))],
) -> PromocionOut:
    """Crea una promoción. Código repetido → 409 `codigo_duplicado`; porcentaje fuera de 1–99 o períodos > 1 → 422."""
    from pack_subscriptions import promociones

    _tenant(principal)
    try:
        return _promocion_out(await promociones.crear(_datos_de_promocion(body), actor=_actor(principal)))
    except promociones.PromoError as exc:
        raise _api_error_promocion(exc) from exc


@router.put("/promociones/{promocion_id}", response_model=PromocionOut)
async def editar_promocion(
    promocion_id: uuid.UUID,
    body: PromocionBody,
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones", write=True))],
) -> PromocionOut:
    """Reemplaza una promoción. Los canjes ya hechos conservan lo que se descontó."""
    from pack_subscriptions import promociones

    _tenant(principal)
    try:
        return _promocion_out(await promociones.actualizar(promocion_id, _datos_de_promocion(body), actor=_actor(principal)))
    except promociones.PromoError as exc:
        raise _api_error_promocion(exc) from exc


@router.delete("/promociones/{promocion_id}", status_code=204)
async def borrar_promocion(
    promocion_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones", write=True))],
) -> None:
    """Borra la promoción (lógico): su código deja de aplicar."""
    from pack_subscriptions import promociones

    _tenant(principal)
    try:
        await promociones.borrar(promocion_id, actor=_actor(principal))
    except promociones.PromoError as exc:
        raise _api_error_promocion(exc) from exc


@router.get("/promociones/{promocion_id}/canjes", response_model=Page[CanjeOut])
async def canjes_de_promocion(
    promocion_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_subscriptions:suscripciones"))],
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[CanjeOut]:
    """Quién canjeó la promoción, en qué estado está cada canje y cuánto se descontó."""
    from pack_subscriptions import promociones

    _tenant(principal)
    try:
        filas, total = await promociones.canjes_de(promocion_id, limit=params.page_size, offset=params.offset)
    except promociones.PromoError as exc:
        raise _api_error_promocion(exc) from exc
    items = [
        CanjeOut(
            id=canje.id,
            estado=canje.estado,
            cliente_id=canje.customer_id,
            suscripcion_id=canje.subscription_id,
            cargo_id=canje.charge_id,
            monto_bruto=float(cargo.monto_bruto) if cargo is not None and cargo.monto_bruto is not None else None,
            descuento=(
                float(cargo.monto_bruto - cargo.monto) if cargo is not None and cargo.monto_bruto is not None else None
            ),
            monto=float(cargo.monto) if cargo is not None else None,
            creado=canje.created_at,
        )
        for canje, cargo in filas
    ]
    return build_page(items, total, params)


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
EstadoSub = Literal["pendiente_pago", "trialing", "activa", "pausada", "vencida", "cancelada"]
EstadoCargo = Literal["pendiente", "pagado", "anulado"]
TipoCargo = Literal["periodo", "cambio_plan", "inscripcion"]
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
    Es un contacto de cobro: solo dentro del horario legal del negocio y si el cliente no
    recibió otro aviso de cobro ese día (HU-SUB-18, Ley 2300 de 2023); si no, 409.
    """
    from sqlalchemy import select

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.events.core_handlers import _whatsapp_conversation_for
    from core.events.outbox import emit_domain
    from core.shared.events import ON_SUBSCRIPTION_CHARGE_REMINDER
    from core.subscriptions import SubscriptionError
    from pack_subscriptions.capability import SubscriptionCapabilityImpl, _config
    from pack_subscriptions.horario_legal import en_horario_de_cobro, hora_local
    from pack_subscriptions.jobs import _payload_cargo, reservar_aviso_del_dia, zona_del_negocio
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
            tz = await zona_del_negocio(session, _tenant(principal))
            if not en_horario_de_cobro(ahora, tz, config.ventana_cobro, config.pais_festivos):
                raise ApiError(
                    409,
                    "fuera_de_horario_de_cobro",
                    "Fuera del horario legal de cobro (lunes a viernes de 7:00 a 19:00, sábados de "
                    "8:00 a 15:00, sin domingos ni festivos). Copia el link o reenvíalo en ese horario.",
                )
            if not await reservar_aviso_del_dia(
                session, _tenant(principal), sub.customer_id, hora_local(ahora, tz).date(),
                ON_SUBSCRIPTION_CHARGE_REMINDER, cargo.id,
            ):
                raise ApiError(
                    409,
                    "aviso_de_cobro_del_dia_enviado",
                    "Este cliente ya recibió un aviso de cobro hoy: la ley permite uno por día. "
                    "Copia el link o reenvíalo mañana.",
                )
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


@router.post("/cargos/{charge_id}/registrar-pago", response_model=PagoRegistradoOut)
async def registrar_pago_por_transferencia(
    charge_id: uuid.UUID,
    body: RegistrarPagoBody,
    principal: Annotated[Principal, Depends(require(SECCION, write=True))],
) -> PagoRegistradoOut:
    """El cliente pagó el cobro por transferencia: lo marca `pagado` con la referencia (HU-SUB-26).

    Reusa `registrar_pago_externo` del proveedor (no hay otro camino de pago): el cargo queda pagado
    con `pago_externo.canal = transferencia`, la suscripción se reactiva si estaba vencida o pausada
    por mora, el período siguiente se calcula desde el pagado y sale `subscription.charge_paid` como
    con un pago por link. El pack sigue cobrando los períodos siguientes. Idempotente por
    referencia; un cargo ya pagado con otra referencia es 409. Auditada con el usuario.
    """
    from sqlalchemy import select

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.subscriptions import SubscriptionError
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.models import CANAL_TRANSFERENCIA, Subscription, SubscriptionCharge
    from pack_subscriptions.periodos import sumar_periodo

    tenant = _tenant(principal)
    referencia = body.referencia.strip()
    if len(referencia) < 3:
        raise ApiError(422, "referencia_invalida", "Indica el número del comprobante o una referencia de al menos 3 caracteres.")
    async with get_session() as session:
        fila = (
            await session.execute(
                select(SubscriptionCharge, Subscription)
                .join(Subscription, Subscription.id == SubscriptionCharge.subscription_id)
                .where(
                    SubscriptionCharge.tenant_id == tenant,
                    Subscription.tenant_id == tenant,
                    SubscriptionCharge.id == charge_id,
                    SubscriptionCharge.deleted_at.is_(None),
                    Subscription.deleted_at.is_(None),
                )
            )
        ).first()
    if fila is None:
        raise ApiError.not_found("Cargo no encontrado.")
    cargo, sub = fila
    if cargo.tipo != "periodo":
        raise ApiError(409, "cargo_no_registrable", "Solo se registra el pago de un cobro de período.")
    if cargo.estado == "anulado":
        raise ApiError(409, "cargo_anulado", "Este cobro está anulado: no se puede registrar un pago.")
    if cargo.estado == "pagado" and (cargo.pago_externo or {}).get("referencia") != referencia:
        raise ApiError(409, "cargo_ya_pagado", "Este cobro ya está pagado.")
    if cargo.estado != "pagado" and sub.estado == "cancelada":
        raise ApiError(409, "suscripcion_cancelada", "La suscripción está cancelada: no hay nada que cobrar.")

    inicio = cargo.fecha_vencimiento or datetime.now(UTC)
    try:
        resultado = await SubscriptionCapabilityImpl().registrar_pago_externo(
            sub.id,
            inicio=inicio,
            fin=sumar_periodo(inicio, sub.periodicidad, sub.dia_ancla),
            referencia=referencia,
            canal=CANAL_TRANSFERENCIA,
            emitir_pago=True,
            cargo_id=cargo.id,
        )
    except SubscriptionError as exc:
        raise ApiError(409, exc.code or "error_suscripcion", str(exc)) from exc
    if resultado["estado"] == "duplicado":
        raise ApiError(409, "cargo_ya_pagado", "Este cobro ya está pagado.")

    async with get_session() as session:
        sub_actual = (
            await session.execute(
                select(Subscription).where(Subscription.tenant_id == tenant, Subscription.id == sub.id)
            )
        ).scalar_one()
        if resultado["estado"] == "registrado":  # repetir el mismo registro no vuelve a auditar
            session.add(
                AuditLog(
                    tenant_id=tenant,
                    actor=_actor(principal),
                    action="subs_pago_transferencia_registrado",
                    input={"subscription_id": str(sub.id), "charge_id": str(cargo.id), "referencia": referencia},
                    output={"periodo": resultado["periodo"]},
                )
            )
    return PagoRegistradoOut(
        estado=resultado["estado"],
        charge_id=cargo.id,
        periodo=resultado["periodo"],
        suscripcion_estado=sub_actual.estado,
        proximo_cargo=sub_actual.proximo_cargo,
    )

