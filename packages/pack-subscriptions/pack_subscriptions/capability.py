"""Implementación del contrato `SubscriptionCapability` v1 (HU-SUB-01, ADR-0106).

Proveedor de referencia sobre `pack_subscriptions_subscriptions`/`_charges`
(ADR-0083: planes como productos de commerce). La lógica de ciclo de vida y de
cobro vivía en `skills.py` (tools propias del pack); desde HU-SUB-01 las tools
`sub_*` están en el core y esta clase es el proveedor que las atiende: el pack
ya NO expone skills al LLM.

Regla del contrato: `charge_link` nunca crea un período futuro.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select

from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from core.subscriptions import (
    ChargeLink,
    PlanSnapshot,
    SubscriptionError,
    SubscriptionView,
)
from pack_subscriptions.models import Subscription, SubscriptionCharge
from pack_subscriptions.periodos import PERIODICIDADES, etiqueta_periodo, sumar_periodo

log = get_logger(__name__)

PLUGIN_ID = "pack_subscriptions"
_ESTADOS_VIGENTES = ("trialing", "activa", "pausada")


async def _config():  # type: ignore[no-untyped-def]
    """Config del pack para el negocio actual; defaults si no hay o no carga."""
    from core.plugin_manager import plugin_manager
    from pack_subscriptions.config import SubscriptionConfig

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None
    return config if isinstance(config, SubscriptionConfig) else SubscriptionConfig()


def _periodicidad(valor: object, por_defecto: str) -> str:
    texto = str(valor or "").strip().lower()
    return texto if texto in PERIODICIDADES else por_defecto


def _en_prueba(sub: Subscription, ahora: datetime) -> bool:
    return sub.estado == "trialing" and sub.trial_termina is not None and sub.trial_termina > ahora


def _view(sub: Subscription, cargos: list[SubscriptionCharge] | None = None) -> SubscriptionView:
    return {
        "subscription_id": str(sub.id),
        "plan_code": str(sub.variante_id) if sub.variante_id else "",
        "plan": sub.concepto,
        "estado": sub.estado,
        "valor_periodo": float(sub.valor_periodo or 0),
        "moneda": sub.moneda,
        "proximo_cargo": sub.proximo_cargo.isoformat() if sub.proximo_cargo else None,
        "trial_termina": sub.trial_termina.isoformat() if sub.trial_termina else None,
        "cargos": [
            {"periodo": c.periodo, "monto": float(c.monto or 0), "estado": c.estado}
            for c in (cargos or [])
        ],
        "referencia": sub.referencia_origen,
        "cambio_programado": sub.cambio_programado,
    }


# Referencias que no vienen de un pedido: no se guardan como `referencia_origen`.
_REFERENCIAS_INTERNAS = {"", "admin-manual", "journey"}


def aplicar_plan(sub: Subscription, datos: dict[str, Any], ahora: datetime) -> None:
    """Deja la suscripción en el plan `datos` ({"variante_id", "plan", "desde_cero"}).

    `desde_cero` (venía de un plan de $0): arranca un período nuevo hoy. Si no, el
    período en curso sigue igual (upgrade a mitad de período o downgrade al corte).
    """
    plan = datos.get("plan") or {}
    try:
        sub.variante_id = uuid.UUID(str(datos.get("variante_id")))
    except (ValueError, TypeError):
        pass
    sub.concepto = plan.get("nombre") or sub.concepto
    sub.valor_periodo = Decimal(str(plan.get("precio") or 0))
    sub.moneda = plan.get("moneda") or sub.moneda
    sub.periodicidad = _periodicidad(plan.get("periodicidad"), sub.periodicidad)
    sub.cambio_programado = None
    if datos.get("desde_cero"):
        sub.estado = "activa"
        sub.trial_termina = None
        sub.dia_ancla = ahora.day
        sub.periodo_actual_inicio = ahora
        sub.proximo_cargo = sumar_periodo(ahora, sub.periodicidad, ahora.day)


def reactivar_por_pago(sub: Subscription, cargo: SubscriptionCharge, *, fin: datetime | None = None) -> None:
    """El cargo de un período quedó pagado (por el link del pack o por otro canal): reactiva
    una vencida, una prueba o una pausada POR MORA (HU-SUB-05; una pausa que pidió el cliente
    sigue pausada) y el período siguiente se calcula desde el pagado.

    `fin` (HU-SH-04): dónde cortó el período el canal que cobró. Si no coincide con el
    calendario de la suscripción (el otro canal arrancó un período nuevo), manda ese corte y
    el día ancla pasa a ser el del período pagado.
    """
    reactivable = sub.estado in ("vencida", "trialing", "activa") or (
        sub.estado == "pausada" and sub.motivo_pausa == "mora"
    )
    if not reactivable:
        return
    sub.estado = "activa"
    sub.motivo_pausa = None
    inicio = cargo.fecha_vencimiento or datetime.now(UTC)
    siguiente = sumar_periodo(inicio, sub.periodicidad, sub.dia_ancla)
    if fin is not None and fin != siguiente:
        sub.dia_ancla = inicio.day
        siguiente = fin
    if sub.periodo_actual_inicio is None or sub.periodo_actual_inicio < inicio:
        sub.periodo_actual_inicio = inicio
    if sub.proximo_cargo is None or sub.proximo_cargo < siguiente:
        sub.proximo_cargo = siguiente


async def crear_suscripcion_para(
    *, customer_id: uuid.UUID, variante_id: uuid.UUID | None, plan: dict[str, Any], reference: str = ""
) -> Subscription:
    """Crea/activa la suscripción de un `Customer` a un plan (idempotente por cliente).

    `plan` es el snapshot aportado por commerce/admin: nombre, precio, moneda,
    periodicidad, trial_dias. Este pack NO guarda ni lee el catálogo.

    HU-SUB-06 (D-16): solo un plan de $0 tiene prueba. Si el cliente ya tiene una
    suscripción vigente a OTRO plan (p. ej. Free y compra Basic), se cambia a ese plan
    y nace `activa` con el período en curso pagado; antes se devolvía la Free sin tocar.
    Cada activación emite `subscription.activated` por el outbox.
    """
    precio = Decimal(str(plan.get("precio") or 0))
    trial_dias = int(plan.get("trial_dias") or 0) if precio == 0 else 0
    referencia = reference.strip() if reference and reference.strip() not in _REFERENCIAS_INTERNAS else None
    config = await _config()
    async with get_session() as session:
        if referencia:
            # HU-SUB-07: el mismo pedido procesado dos veces no crea dos suscripciones.
            previa = (
                await session.execute(
                    select(Subscription).where(
                        Subscription.tenant_id == get_current_tenant_id(),
                        Subscription.referencia_origen == referencia,
                        # Un pedido con dos planes crea dos suscripciones (auditoría).
                        Subscription.variante_id.is_(None) if variante_id is None else Subscription.variante_id == variante_id,
                        Subscription.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if previa is not None:
                return previa
        estados = Subscription.estado.in_(_ESTADOS_VIGENTES)
        if precio > 0:
            # HU-SH-04 (ADR-0123): quien compra un plan pago después de que venció su Free
            # sigue con SU suscripción (y con su negocio, si lo aprovisionó un vendedor):
            # antes nacía otra suscripción y el alta automática creaba otro negocio.
            estados = or_(estados, and_(Subscription.estado == "vencida", Subscription.valor_periodo == 0))
        filtros = [
            Subscription.tenant_id == get_current_tenant_id(),
            Subscription.customer_id == customer_id,
            estados,
            Subscription.deleted_at.is_(None),
        ]
        if config.varias_por_cliente and variante_id is not None:
            # Varias por cliente (ADR-0111): solo cuenta una vigente del MISMO plan.
            filtros.append(Subscription.variante_id == variante_id)
        existente = (
            await session.execute(
                select(Subscription)
                .where(*filtros)
                # Una vigente antes que una Free vencida.
                .order_by((Subscription.estado == "vencida").asc(), Subscription.created_at.desc())
            )
        ).scalars().first()
        now = datetime.now(UTC)
        periodicidad = _periodicidad(plan.get("periodicidad"), config.periodicidad)
        if existente is not None:
            otra_variante = (
                variante_id is not None
                and existente.variante_id is not None
                and existente.variante_id != variante_id
            )
            if not otra_variante and Decimal(existente.valor_periodo or 0) == precio:
                return existente  # mismo plan: idempotente
            # Cambio de plan al comprar otro (Free → pago): activa con el período pagado.
            existente.variante_id = variante_id
            existente.concepto = plan.get("nombre") or existente.concepto
            existente.valor_periodo = precio
            existente.moneda = plan.get("moneda") or existente.moneda
            existente.periodicidad = periodicidad
            existente.estado = "trialing" if trial_dias else "activa"
            existente.trial_termina = (now + timedelta(days=trial_dias)) if trial_dias else None
            existente.dia_ancla = now.day
            existente.periodo_actual_inicio = now
            existente.proximo_cargo = existente.trial_termina or sumar_periodo(now, periodicidad, now.day)
            existente.motivo_pausa = None
            existente.cambio_programado = None
            existente.referencia_origen = referencia or existente.referencia_origen
            await session.flush()
            await _emitir_activada(session, existente, referencia, cambio=True)
            return existente
        trial_termina = (now + timedelta(days=trial_dias)) if trial_dias else None
        sub = Subscription(
            tenant_id=get_current_tenant_id(),
            customer_id=customer_id,
            variante_id=variante_id,
            concepto=plan.get("nombre") or "Plan",
            estado="trialing" if trial_dias else "activa",
            valor_periodo=precio,
            moneda=plan.get("moneda") or "COP",
            periodicidad=periodicidad,
            dia_ancla=now.day,
            trial_termina=trial_termina,
            periodo_actual_inicio=now,
            # Con prueba, el primer cargo es al terminarla; sin prueba, el período
            # actual ya está pago (lo activó un pedido pagado) y el siguiente vence
            # por calendario (HU-SUB-04).
            proximo_cargo=trial_termina or sumar_periodo(now, periodicidad, now.day),
            fecha_alta=now,
            referencia_origen=referencia,
        )
        session.add(sub)
        await session.flush()
        await _emitir_activada(session, sub, referencia, cambio=False)
        return sub


async def _emitir_activada(session, sub: Subscription, referencia: str | None, *, cambio: bool) -> None:
    """`subscription.activated` por el outbox. `referencia` es el pedido que la activó, o
    None si no hubo pedido (activación manual, journeys, cargo de cambio de plan): así el
    alta automática de un negocio (HU-SH-03, ADR-0121) no tiene que adivinar cuáles son
    referencias internas. `variant_id` dice qué línea del pedido se activó.

    `periodo_inicio`, `proximo_cargo` y `dia_ancla` (HU-SH-04, ADR-0123): el período en curso,
    para que el plan del negocio aprovisionado corte el período el mismo día."""
    from core.events.outbox import emit_domain
    from core.shared.events import ON_SUBSCRIPTION_ACTIVATED

    await emit_domain(
        session,
        tenant_id=sub.tenant_id,
        topic=ON_SUBSCRIPTION_ACTIVATED,
        dedupe_key=f"sub_activated:{sub.id}:{sub.variante_id or sub.concepto}",
        payload={
            "subscription_id": str(sub.id),
            "customer_id": str(sub.customer_id),
            "concepto": sub.concepto,
            "estado": sub.estado,
            "valor_periodo": str(sub.valor_periodo),
            "periodicidad": sub.periodicidad,
            "trial_termina": sub.trial_termina.isoformat() if sub.trial_termina else None,
            "referencia": referencia,
            "cambio_de_plan": cambio,
            "variant_id": str(sub.variante_id) if sub.variante_id else None,
            "periodo_inicio": sub.periodo_actual_inicio.isoformat() if sub.periodo_actual_inicio else None,
            "proximo_cargo": sub.proximo_cargo.isoformat() if sub.proximo_cargo else None,
            "dia_ancla": sub.dia_ancla,
        },
    )


async def _anular_cambios_pendientes(
    session, sub: Subscription, *, salvo: tuple[str, Decimal] | None = None  # type: ignore[no-untyped-def]
) -> SubscriptionCharge | None:
    """Anula los cargos de cambio pendientes de la suscripción (su link deja de aplicar el
    plan: `_on_payment_confirmed` solo procesa cargos pendientes). Devuelve el que coincide
    con `salvo` (variante, monto), que se conserva."""
    previos = (
        await session.execute(
            select(SubscriptionCharge).where(
                SubscriptionCharge.tenant_id == get_current_tenant_id(),
                SubscriptionCharge.subscription_id == sub.id,
                SubscriptionCharge.tipo == "cambio_plan",
                SubscriptionCharge.estado == "pendiente",
                SubscriptionCharge.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    conservado = None
    for previo in previos:
        if salvo and (previo.cambio_plan or {}).get("variante_id") == salvo[0] and Decimal(previo.monto) == salvo[1]:
            conservado = previo
        else:
            previo.estado = "anulado"
    return conservado


async def _link_de_cargo(cargo: SubscriptionCharge, sub: Subscription, descripcion: str) -> dict[str, Any]:
    """Link de pago en línea de un cargo (`sub-<id>`), o {} si no hay pasarela o la
    pasarela no acepta el monto: el cargo queda pendiente para cobrarlo por transferencia."""
    if cargo.link_url and cargo.referencia:
        return {"url": cargo.link_url, "referencia": cargo.referencia}
    from core.agents.spec import enabled_packs, load_active_spec
    from core.payments import PaymentCapabilityError, payment_registry

    cap = payment_registry.capability_for(enabled_packs(await load_active_spec()))
    if cap is None:
        return {}
    try:
        link = await cap.create_link(reference=f"sub-{cargo.id}", amount=float(cargo.monto or 0), description=descripcion)
    except PaymentCapabilityError:
        log.info("subs_cargo_sin_link", charge_id=str(cargo.id), monto=str(cargo.monto))
        return {}
    async with get_session() as session:
        fila = await session.get(SubscriptionCharge, cargo.id)
        if fila is not None:
            fila.referencia = link.reference
            fila.link_url = link.url
    return {"url": link.url, "referencia": link.reference}


class SubscriptionCapabilityImpl:
    """Proveedor de suscripciones del contrato v1 (las 8 operaciones)."""

    provider_id = PLUGIN_ID

    async def _sub_vigente(self, session, subscription_id: uuid.UUID) -> Subscription:
        sub = await session.get(Subscription, subscription_id)
        # `tenant_id` explícito además de RLS (defensa en profundidad; auditoría HU-SUB-12).
        if sub is not None and sub.tenant_id != get_current_tenant_id():
            sub = None
        if sub is None or sub.deleted_at is not None or sub.estado == "cancelada":
            raise SubscriptionError(
                "La suscripción no existe o está cancelada.", code="suscripcion_no_encontrada"
            )
        return sub

    async def _cargos_recientes(
        self, session, subscription_id: uuid.UUID
    ) -> list[SubscriptionCharge]:
        return (
            await session.execute(
                select(SubscriptionCharge)
                .where(
                    SubscriptionCharge.tenant_id == get_current_tenant_id(),
                    SubscriptionCharge.subscription_id == subscription_id,
                    SubscriptionCharge.deleted_at.is_(None),
                )
                .order_by(SubscriptionCharge.created_at.desc())
                .limit(6)
            )
        ).scalars().all()

    async def activate(
        self,
        *,
        customer_id: uuid.UUID,
        variant_id: str,
        plan: PlanSnapshot,
        reference: str = "",
    ) -> SubscriptionView:
        variante_id = None
        try:
            variante_id = uuid.UUID(str(variant_id))
        except (ValueError, TypeError):
            pass
        sub = await crear_suscripcion_para(
            customer_id=customer_id, variante_id=variante_id, plan=plan, reference=reference
        )
        return _view(sub)

    async def get(self, subscription_id: uuid.UUID) -> SubscriptionView:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            cargos = await self._cargos_recientes(session, subscription_id)
            return _view(sub, cargos)

    async def list_for_customer(self, customer_id: uuid.UUID) -> list[SubscriptionView]:
        async with get_session() as session:
            subs = (
                await session.execute(
                    select(Subscription)
                    .where(
                        Subscription.tenant_id == get_current_tenant_id(),
                        Subscription.customer_id == customer_id,
                        Subscription.deleted_at.is_(None),
                    )
                    .order_by(Subscription.created_at.desc())
                )
            ).scalars().all()
            return [_view(s) for s in subs]

    async def pause(
        self, subscription_id: uuid.UUID, *, reason: str = "", actor: str = ""
    ) -> SubscriptionView:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            sub.estado = "pausada"
            sub.motivo_pausa = "cliente"
            sub.proximo_cargo = None
            await session.flush()
            return _view(sub)

    async def resume(self, subscription_id: uuid.UUID, *, actor: str = "") -> SubscriptionView:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            sub.estado = "activa"
            sub.motivo_pausa = None
            sub.fecha_cancelacion = None
            if sub.proximo_cargo is None:
                sub.proximo_cargo = sumar_periodo(datetime.now(UTC), sub.periodicidad, sub.dia_ancla)
            await session.flush()
            return _view(sub)

    async def cancel(
        self, subscription_id: uuid.UUID, *, reason: str = "", actor: str = ""
    ) -> SubscriptionView:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            sub.estado = "cancelada"
            sub.fecha_cancelacion = datetime.now(UTC)
            sub.proximo_cargo = None
            await session.flush()
            return _view(sub)

    async def permite_varias(self) -> bool:
        """Operación opcional del contrato (ADR-0111): ¿varias suscripciones por cliente?"""
        return bool((await _config()).varias_por_cliente)

    async def change_plan(
        self,
        subscription_id: uuid.UUID,
        *,
        variant_id: str,
        plan: PlanSnapshot,
    ) -> SubscriptionView:
        """Cambio de plan con el snapshot del CATÁLOGO (HU-SUB-07, ADR-0111).

        - upgrade: cargo `cambio_plan` por la diferencia prorrateada (o el precio completo si
          venía de un plan de $0); el plan cambia al pagarlo (`_on_payment_confirmed`);
        - downgrade: `cambio_programado` para el próximo período, sin reembolso;
        - mismo precio: inmediato.
        """
        from pack_subscriptions.prorrateo import diferencia_prorrateada

        ahora = datetime.now(UTC)
        nuevo = Decimal(str(plan.get("precio") or 0))
        destino = {"variante_id": str(variant_id), "plan": dict(plan)}
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            if sub.variante_id is not None and str(sub.variante_id) == str(variant_id):
                raise SubscriptionError("El cliente ya tiene ese plan.", code="mismo_plan")
            # Auditoría HU-SUB-07: sin cambios desde una pausada o vencida (el período no
            # está vigente y la diferencia se calcularía mal); primero se reactiva.
            if sub.estado not in ("activa", "trialing"):
                raise SubscriptionError(
                    "Solo se puede cambiar de plan con la suscripción activa; primero hay que reactivarla.",
                    code="estado_no_permite_cambio",
                )
            actual = Decimal(sub.valor_periodo or 0)
            desde_cero = actual == 0 or sub.estado == "trialing"
            if nuevo == 0 and desde_cero:
                raise SubscriptionError("Ya está en un plan sin costo.", code="plan_gratis")
            # Precios de periodicidades distintas no se comparan (auditoría): solo desde un
            # plan de $0, que arranca un período nuevo.
            periodicidad_nueva = _periodicidad(plan.get("periodicidad"), sub.periodicidad)
            if not desde_cero and periodicidad_nueva != sub.periodicidad:
                raise SubscriptionError(
                    "El cambio entre periodicidades distintas (p. ej. mensual a anual) se hace con "
                    "una compra nueva al terminar el período.",
                    code="periodicidad_distinta",
                )
            if nuevo < actual:
                await _anular_cambios_pendientes(session, sub)
                sub.cambio_programado = {**destino, "desde": (sub.proximo_cargo or ahora).isoformat()}
                await session.flush()
                return {**_view(sub), "cambio": "programado"}
            if desde_cero:
                monto = nuevo
            else:
                inicio = sub.periodo_actual_inicio or ahora
                fin = sub.proximo_cargo or sumar_periodo(inicio, sub.periodicidad, sub.dia_ancla)
                monto = diferencia_prorrateada(actual, nuevo, inicio, fin, ahora, sub.moneda)
            if monto <= 0:
                await _anular_cambios_pendientes(session, sub)
                aplicar_plan(sub, {**destino, "desde_cero": desde_cero}, ahora)
                await session.flush()
                return {**_view(sub), "cambio": "inmediato"}
            # Un cambio pendiente anterior a OTRO plan (u otro monto) se anula; al mismo se reusa.
            cargo = await _anular_cambios_pendientes(session, sub, salvo=(str(variant_id), monto))
            if cargo is None:
                cargo = SubscriptionCharge(
                    tenant_id=get_current_tenant_id(),
                    subscription_id=sub.id,
                    periodo=f"cambio-{ahora:%Y%m%d%H%M%S}",
                    monto=monto,
                    estado="pendiente",
                    fecha_vencimiento=ahora,
                    tipo="cambio_plan",
                    # El período del cálculo: si al pagar ya es otro, el cambio no se aplica.
                    cambio_plan={
                        **destino,
                        "desde_cero": desde_cero,
                        "proximo_cargo": sub.proximo_cargo.isoformat() if sub.proximo_cargo else None,
                    },
                )
                session.add(cargo)
            await session.flush()
            vista = _view(sub)
        link = await _link_de_cargo(cargo, sub, f"Cambio a {plan.get('nombre')}")
        return {
            **vista,
            "cambio": "pendiente_pago",
            "cargo_cambio": {"monto": float(monto), "link": link.get("url"), "referencia": link.get("referencia")},
        }

    async def charge_link(
        self, subscription_id: uuid.UUID, *, period: str = ""
    ) -> ChargeLink:
        async with get_session() as session:
            sub = await self._sub_vigente(session, subscription_id)
            ahora = datetime.now(UTC)
            if _en_prueba(sub, ahora):
                raise SubscriptionError(
                    "La suscripción está en período de prueba: todavía no hay nada que cobrar.",
                    code="en_prueba",
                )
            inicio_actual = sub.periodo_actual_inicio or ahora
            actual = etiqueta_periodo(inicio_actual, sub.periodicidad)
            if period and period > actual:
                raise SubscriptionError("No se cobra un período futuro.", code="periodo_futuro")
            filtro = [
                SubscriptionCharge.tenant_id == get_current_tenant_id(),
                SubscriptionCharge.subscription_id == subscription_id,
                SubscriptionCharge.tipo == "periodo",  # un cargo de cambio no es un período (ADR-0111)
                SubscriptionCharge.deleted_at.is_(None),
            ]
            if period:
                filtro.append(SubscriptionCharge.periodo == period)
            # El cargo vencido más viejo, o el del período en curso: nunca uno futuro.
            cargo = (
                await session.execute(
                    select(SubscriptionCharge)
                    .where(*filtro, SubscriptionCharge.estado == "pendiente")
                    .order_by(SubscriptionCharge.fecha_vencimiento.asc().nulls_last())
                )
            ).scalars().first()
            periodo = cargo.periodo if cargo is not None else (period or actual)
            if cargo is None:
                pagado = (
                    await session.execute(
                        select(SubscriptionCharge.id).where(
                            *filtro, SubscriptionCharge.periodo == periodo, SubscriptionCharge.estado == "pagado"
                        )
                    )
                ).first()
                if pagado is not None:
                    raise SubscriptionError("Ese período ya está pago.", code="ya_pagado")
                cargo = SubscriptionCharge(
                    tenant_id=get_current_tenant_id(),
                    subscription_id=subscription_id,
                    periodo=periodo,
                    monto=sub.valor_periodo or 0,
                    estado="pendiente",
                    fecha_vencimiento=inicio_actual,
                )
                session.add(cargo)
                await session.flush()

        if cargo.link_url and cargo.referencia:
            # El cron ya generó el link de este cargo (HU-SUB-05): se reusa. Antes se pedía
            # otro con la misma referencia y la pasarela fallaba (journeys de HU-SUB-12).
            return {
                "url": cargo.link_url,
                "referencia": cargo.referencia,
                "periodo": periodo,
                "monto": float(cargo.monto or 0),
            }

        from core.agents.spec import enabled_packs, load_active_spec
        from core.payments import PaymentCapabilityError, payment_registry

        spec = await load_active_spec()
        cap = payment_registry.capability_for(enabled_packs(spec))
        if cap is None:
            raise SubscriptionError(
                "El pago en línea no está configurado para este negocio; coordinemos "
                "el pago por otro medio.",
                code="sin_pasarela",
            )
        reference = f"sub-{cargo.id}"
        try:
            link = await cap.create_link(
                reference=reference,
                amount=float(cargo.monto or 0),
                description=f"Suscripción {sub.concepto}",
            )
        except PaymentCapabilityError as exc:
            raise SubscriptionError(
                "El pago en línea no está disponible para este monto; coordinemos el "
                "pago por otro medio.",
                code="monto_bajo_minimo",
            ) from exc
        async with get_session() as session:
            fila = await session.get(SubscriptionCharge, cargo.id)
            if fila is not None:
                fila.referencia = link.reference
                fila.link_url = link.url
                await session.flush()
        return {
            "url": link.url,
            "referencia": link.reference,
            "periodo": periodo,
            "monto": float(cargo.monto or 0),
        }

    # ------------------------------------------------ operaciones opcionales (ADR-0123)
    async def _sub_bloqueada(self, session, subscription_id: uuid.UUID) -> Subscription:  # type: ignore[no-untyped-def]
        sub = (
            await session.execute(
                select(Subscription)
                .where(
                    Subscription.tenant_id == get_current_tenant_id(),
                    Subscription.id == subscription_id,
                    Subscription.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if sub is None:
            raise SubscriptionError("La suscripción no existe.", code="suscripcion_no_encontrada")
        return sub

    async def _cargo_del_periodo(self, session, sub: Subscription, periodo: str) -> SubscriptionCharge | None:  # type: ignore[no-untyped-def]
        return (
            await session.execute(
                select(SubscriptionCharge)
                .where(
                    SubscriptionCharge.tenant_id == get_current_tenant_id(),
                    SubscriptionCharge.subscription_id == sub.id,
                    SubscriptionCharge.periodo == periodo,
                    SubscriptionCharge.tipo == "periodo",
                    SubscriptionCharge.deleted_at.is_(None),
                )
                .order_by(SubscriptionCharge.created_at.asc())
                .with_for_update()
            )
        ).scalars().first()

    async def registrar_pago_externo(
        self,
        subscription_id: uuid.UUID,
        *,
        inicio: datetime,
        fin: datetime,
        referencia: str,
        canal: str,
    ) -> dict[str, Any]:
        """Operación opcional del contrato (HU-SH-04, ADR-0123): el período que empieza en
        `inicio` lo pagó OTRO canal (p. ej. el panel de la plataforma). Su cargo queda pagado
        (se crea si todavía no existía) y la suscripción se reactiva como con un pago propio:
        el asistente no lo vuelve a cobrar ni a recordar. Idempotente por `referencia`.

        Devuelve `{"estado", "periodo", "charge_id"}`. `estado`: `registrado`, `ya_registrado`
        o `duplicado` (el cargo ya estaba pagado por el link del pack u otro pago).
        """
        ahora = datetime.now(UTC)
        externo = {"canal": canal, "referencia": referencia, "registrado_en": ahora.isoformat()}
        async with get_session() as session:
            sub = await self._sub_bloqueada(session, subscription_id)
            periodo = etiqueta_periodo(inicio, sub.periodicidad)
            cargo = await self._cargo_del_periodo(session, sub, periodo)
            if cargo is not None and cargo.estado == "pagado":
                previo = cargo.pago_externo or {}
                estado = "ya_registrado" if previo.get("referencia") == referencia else "duplicado"
                return {"estado": estado, "periodo": periodo, "charge_id": str(cargo.id)}
            if cargo is None:
                cargo = SubscriptionCharge(
                    tenant_id=get_current_tenant_id(),
                    subscription_id=sub.id,
                    periodo=periodo,
                    monto=sub.valor_periodo or 0,
                    estado="pagado",
                    fecha_vencimiento=inicio,
                    pago_externo=externo,
                )
                session.add(cargo)
            else:
                cargo.estado = "pagado"
                cargo.pago_externo = externo
            await session.flush()
            reactivar_por_pago(sub, cargo, fin=fin)
            await session.flush()
            log.info("subs_pago_externo_registrado", subscription_id=str(sub.id), periodo=periodo, canal=canal)
            return {"estado": "registrado", "periodo": periodo, "charge_id": str(cargo.id)}
