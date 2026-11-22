"""Inicio y confirmación de la suscripción de un tenant a un plan pago (ADR-0046).

Es el **canal panel** del cobro de la plataforma (HU-SH-04, ADR-0123):

- `start_subscription(plan_id)`: el link de la plataforma para el período que toca pagar
  (`periodo_a_cobrar`), con su referencia `plat-<slug>-<AAAAMM>`. Si ese período ya está
  pagado (por el panel o por el asistente) responde «ya pagado» y no crea otro link.
- `apply_payment(provider_reference, status, ...)`: idempotente, invocado por el webhook de
  billing. Al aprobar, aplica el período con `aplicar_pago_de_periodo` (la misma puerta que
  usa el asistente) y avisa por el outbox para la sincronización inversa
  (`platform.period_paid`): el cargo del mismo período en el canal asistente queda pagado.
- `start_recarga(paquete_id)` (ADR-0209 §4, HU-BILL-23): el link de un paquete de créditos con el
  descuento del plan vigente. Es otra fila de `subscription_payments` (`concepto = 'recarga'`, sin
  período): entra por el mismo webhook y `apply_payment` la acredita como `credit_grant` del ciclo
  actual, sin tocar el período, el plan ni el estado de la suscripción.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.billing.periodos import Periodo, periodo_a_cobrar, referencia_del_periodo, ventana_mensual
from core.billing.plans import (
    Plan,
    descuento_de_recarga,
    get_paquete_de_recarga,
    get_plan,
    precio_de_recarga,
)
from core.billing.providers import get_billing_provider
from core.billing.providers.base import BillingRequestRejected, MontoBajoElMinimo
from core.config import get_settings
from core.db.models import CreditGrant, SubscriptionPayment, Tenant, TenantSubscription, utcnow
from core.db.session import get_owned, get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)

# Un link de Wompi vence a las 24 h: hasta 20 h se devuelve el mismo para el mismo período,
# así el cliente no tiene dos links vivos para pagar lo mismo.
_REUSO_DEL_LINK = timedelta(hours=20)
# El cobro que deja la renovación (sin link todavía) se reconoce por este proveedor.
PROVEEDOR_RENOVACION = "plataforma"


class BillingNotConfigured(Exception):
    """La pasarela de pago no tiene credenciales (BILLING_PROVIDER=none)."""


class PeriodoYaPagado(BillingRequestRejected):
    """El período que toca pagar ya está pagado (HU-SH-04): no se crea otro link."""

    code = "ya_pagado"

    def __init__(self, referencia: str, canal: str | None = None, *, hasta: datetime | None = None) -> None:
        self.referencia = referencia
        self.canal = canal
        self.hasta = hasta
        por = f" por el {canal}" if canal else ""
        siguiente = ""
        if hasta is not None:
            siguiente = (
                f" Tu plan está al día hasta el {hasta:%d/%m/%Y}; el cobro del período siguiente "
                "se genera unos días antes."
            )
        super().__init__(f"El período {referencia} ya está pagado{por}.{siguiente}")


class CambioDePlanEnElAsistente(BillingRequestRejected):
    """El negocio lo creó el asistente de un vendedor: su plan cambia por el asistente
    (ADR-0123 § 6); por el panel solo se paga el período del plan actual."""

    code = "cambio_en_el_asistente"

    def __init__(self) -> None:
        super().__init__(
            "Tu plan se gestiona con el asistente de SolversHub: para cambiarlo, escríbele por "
            "WhatsApp. Desde aquí puedes pagar el período de tu plan actual."
        )


def _miles(valor: int) -> str:
    return f"{valor:,}".replace(",", ".")


class RecargaNoDisponible(BillingRequestRejected):
    """El negocio no puede comprar recargas (ADR-0209 §4): sin suscripción activa o en Free."""

    code = "recarga_no_disponible"

    def __init__(self) -> None:
        super().__init__(
            "Las recargas de créditos son para negocios con un plan pago activo. "
            "Elige o paga un plan para poder recargar."
        )


class RecargaBajoElMinimo(BillingRequestRejected):
    """El precio del paquete es menor que el mínimo de Wompi del ambiente: en el sandbox (150.000 COP)
    casi ningún paquete se puede pagar. Se rechaza antes de crear el link (no queda fila pendiente)."""

    code = "recarga_bajo_minimo"

    def __init__(self, precio_cop: int, minimo_cop: int) -> None:
        self.precio_cop = precio_cop
        self.minimo_cop = minimo_cop
        super().__init__(
            f"Esta recarga cuesta ${_miles(precio_cop)} COP y en este ambiente la pasarela solo acepta pagos "
            f"desde ${_miles(minimo_cop)} COP, así que no se puede pagar aquí."
        )


def _canal_de(provider: str | None) -> str:
    return "asistente" if provider == "asistente" else "panel"


async def _suscripcion(session, tenant_id: uuid.UUID) -> TenantSubscription:  # type: ignore[no-untyped-def]
    sub = (
        await session.execute(
            select(TenantSubscription).where(
                TenantSubscription.tenant_id == tenant_id,
                TenantSubscription.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if sub is None:
        from core.billing.service import ensure_free_subscription

        sub = await ensure_free_subscription(session, tenant_id)
    return sub


async def _slug(session, tenant_id: uuid.UUID) -> str:  # type: ignore[no-untyped-def]
    tenant = await session.get(Tenant, tenant_id)
    return tenant.slug if tenant is not None else tenant_id.hex[:8]


async def start_subscription(plan_id: str) -> str | None:
    """Link de pago del período que toca pagar. Devuelve la URL o None (sin pasarela).

    Lanza `PeriodoYaPagado` si el período ya está pagado por cualquiera de los dos canales y
    `CambioDePlanEnElAsistente` si un negocio del alta automática pide otro plan."""
    from core.billing.canales import vinculo_asistente

    plan = get_plan(plan_id)
    if plan is None:
        raise BillingRequestRejected(f"Plan desconocido: {plan_id}")
    if plan.is_free:
        raise BillingRequestRejected("El plan free no requiere pago")
    provider = get_billing_provider()
    if provider is None:
        return None

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise BillingNotConfigured("Sin tenant en contexto")

    vinculo = await vinculo_asistente(tenant_id)
    now = datetime.now(UTC)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            sub = await _suscripcion(session, tenant_id)
            if vinculo is not None and plan.id != sub.plan_id:
                raise CambioDePlanEnElAsistente()
            slug = await _slug(session, tenant_id)
            periodo = periodo_a_cobrar(
                slug=slug,
                plan_id=sub.plan_id,
                status=sub.status,
                period_end=sub.period_end,
                dia_ancla=sub.dia_ancla,
                now=now,
            )
            ventana = timedelta(days=get_settings().billing_dias_renovacion)
            if not periodo.desde_cero and plan.id == sub.plan_id and periodo.inicio > now + ventana:
                # Al día: lo que toca pagar ya está pagado y el período siguiente todavía no se
                # cobra (se genera `BILLING_DIAS_RENOVACION` días antes). No se adelantan
                # períodos sin límite: así un período pagado por un canal no se cobra en el otro.
                ultimo = (
                    await session.execute(
                        select(SubscriptionPayment)
                        .where(
                            SubscriptionPayment.tenant_id == tenant_id,
                            SubscriptionPayment.status == "aprobado",
                            SubscriptionPayment.periodo.isnot(None),
                            SubscriptionPayment.deleted_at.is_(None),
                        )
                        .order_by(SubscriptionPayment.period_start.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                referencia = ultimo.periodo if ultimo is not None and ultimo.periodo else None
                raise PeriodoYaPagado(
                    referencia or referencia_del_periodo(slug, sub.period_start),
                    _canal_de(ultimo.provider) if ultimo is not None else None,
                    hasta=sub.period_end,
                )
            intentos = (
                await session.execute(
                    select(SubscriptionPayment)
                    .where(
                        SubscriptionPayment.tenant_id == tenant_id,
                        SubscriptionPayment.periodo == periodo.referencia,
                        SubscriptionPayment.deleted_at.is_(None),
                    )
                    .order_by(SubscriptionPayment.updated_at.desc())
                )
            ).scalars().all()
            pagado = next((p for p in intentos if p.status == "aprobado"), None)
            if pagado is not None:
                raise PeriodoYaPagado(periodo.referencia, _canal_de(pagado.provider))
            vigente = next(
                (
                    p
                    for p in intentos
                    if p.status == "pendiente"
                    and p.checkout_url
                    and p.plan_id == plan.id
                    and p.provider == provider.provider_name
                    and p.updated_at > now - _REUSO_DEL_LINK
                ),
                None,
            )
            if vigente is not None:
                log.info("subscription_checkout_reusado", tenant_id=str(tenant_id), referencia=periodo.referencia)
                return vigente.checkout_url
            renovacion = next(
                (p.id for p in intentos if p.status == "pendiente" and p.provider == PROVEEDOR_RENOVACION),
                None,
            )
            sub_id = sub.id
            await session.commit()

    if vinculo is not None:
        # El asistente pudo haber cobrado el período y el evento todavía no llegó al negocio.
        from core.billing.canales import estado_en_el_asistente

        if await estado_en_el_asistente(vinculo, periodo.etiqueta) == "pagado":
            raise PeriodoYaPagado(periodo.referencia, "asistente")

    reference = f"sub-{tenant_id.hex[:8]}-{uuid.uuid4().hex[:10]}"
    # El link se crea ANTES de persistir el pago: si la red falla no queda una
    # fila "pendiente" huérfana; si el link existe, el webhook siempre encontrará
    # la fila por `provider_reference` (evita cobro sin activación).
    link = await provider.create_payment_link(
        reference=reference, amount_cop=plan.price_cop, plan_name=plan.name
    )

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            payment = await get_owned(session, SubscriptionPayment, renovacion, tenant_id) if renovacion else None
            if payment is None or payment.status != "pendiente" or payment.checkout_url:
                payment = SubscriptionPayment(tenant_id=tenant_id, subscription_id=sub_id, status="pendiente")
                session.add(payment)
            # El cobro de la renovación (sin link) pasa a ser este intento: un solo pendiente.
            payment.plan_id = plan.id
            payment.amount_cop = plan.price_cop
            payment.currency = plan.currency
            payment.provider = provider.provider_name
            payment.provider_reference = link.provider_reference
            payment.checkout_url = link.url
            payment.periodo = periodo.referencia
            payment.period_start = periodo.inicio
            payment.period_end = periodo.fin
            payment.updated_at = utcnow()
            await session.commit()
    log.info(
        "subscription_checkout_started",
        tenant_id=str(tenant_id),
        plan=plan.id,
        referencia=periodo.referencia,
    )
    return link.url


def recarga_disponible(sub: TenantSubscription | None) -> Plan | None:
    """El plan con el que el negocio compra recargas, o None si no puede (ADR-0209 §4): hace falta una
    suscripción `active` en un plan pago que defina `descuento_recarga` (Free no compra). Un negocio en
    `past_due`, vencido o cancelado primero paga su plan."""
    if sub is None or sub.deleted_at is not None or sub.status != "active":
        return None
    plan = get_plan(sub.plan_id or "")
    if plan is None or plan.is_free:
        return None
    return plan if descuento_de_recarga(plan) is not None else None


async def start_recarga(paquete_id: str) -> str | None:
    """Link de pago de un paquete de recarga (ADR-0209 §4, HU-BILL-23). Devuelve la URL o None (sin pasarela).

    El precio lo arma el servidor: el de lista del catálogo menos `features.descuento_recarga` del plan
    vigente. Lanza `RecargaNoDisponible` sin suscripción activa o en Free, y `RecargaBajoElMinimo` si el
    precio queda bajo el mínimo de Wompi del ambiente (sandbox). Los negocios que pagan el plan por el
    asistente también recargan por aquí. Un pedido repetido del mismo paquete en menos de 20 h devuelve
    el mismo link."""
    paquete = get_paquete_de_recarga(paquete_id)
    if paquete is None:
        raise BillingRequestRejected(f"Paquete de recarga desconocido: {paquete_id}")
    provider = get_billing_provider()
    if provider is None:
        return None
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise BillingNotConfigured("Sin tenant en contexto")

    now = datetime.now(UTC)
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(TenantSubscription).where(
                        TenantSubscription.tenant_id == tenant_id,
                        TenantSubscription.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            plan = recarga_disponible(sub)
            precio = precio_de_recarga(paquete, plan) if plan is not None else None
            if sub is None or plan is None or precio is None:
                raise RecargaNoDisponible()
            creditos = Decimal(paquete.creditos)
            vigente = (
                await session.execute(
                    select(SubscriptionPayment)
                    .where(
                        SubscriptionPayment.tenant_id == tenant_id,
                        SubscriptionPayment.concepto == "recarga",
                        SubscriptionPayment.status == "pendiente",
                        SubscriptionPayment.creditos == creditos,
                        SubscriptionPayment.amount_cop == precio,
                        SubscriptionPayment.plan_id == plan.id,
                        SubscriptionPayment.provider == provider.provider_name,
                        SubscriptionPayment.checkout_url.isnot(None),
                        SubscriptionPayment.updated_at > now - _REUSO_DEL_LINK,
                        SubscriptionPayment.deleted_at.is_(None),
                    )
                    .order_by(SubscriptionPayment.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if vigente is not None:
                log.info("recarga_checkout_reusado", tenant_id=str(tenant_id), paquete=paquete.id)
                return vigente.checkout_url
            sub_id = sub.id

    reference = f"rec-{tenant_id.hex[:8]}-{uuid.uuid4().hex[:10]}"
    # Como en `start_subscription`: el link se crea ANTES de persistir el pago (sin filas huérfanas si la
    # red falla); el mínimo de Wompi se revisa antes de la red.
    try:
        link = await provider.create_payment_link(
            reference=reference,
            amount_cop=precio,
            plan_name=plan.name,
            concepto="recarga",
            descripcion=f"Recarga de {_miles(paquete.creditos)} créditos — SolversHub",
        )
    except MontoBajoElMinimo as exc:
        raise RecargaBajoElMinimo(precio, exc.minimo_cop) from exc

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            session.add(
                SubscriptionPayment(
                    tenant_id=tenant_id,
                    subscription_id=sub_id,
                    status="pendiente",
                    concepto="recarga",
                    creditos=creditos,
                    plan_id=plan.id,
                    amount_cop=precio,
                    currency=plan.currency,
                    provider=provider.provider_name,
                    provider_reference=link.provider_reference,
                    checkout_url=link.url,
                    periodo=None,
                    updated_at=utcnow(),
                )
            )
            await session.commit()
    log.info(
        "recarga_checkout_started",
        tenant_id=str(tenant_id),
        paquete=paquete.id,
        plan=plan.id,
        precio_cop=precio,
    )
    return link.url


async def _acreditar_recarga(session, tid: uuid.UUID, payment: SubscriptionPayment, sub: TenantSubscription) -> str:  # type: ignore[no-untyped-def]
    """Inserta el `credit_grant` de una recarga aprobada en el ciclo actual (ADR-0209 §4), una sola vez:
    `origen` = id del pago y `ON CONFLICT DO NOTHING` sobre `(tenant_id, ciclo, tipo, origen)`. No toca el
    período, el plan ni el estado de la suscripción. Devuelve `acreditada`, `ya_acreditada` (el abono ya
    estaba) o `sin_abono` (sin ciclo o sin créditos: se alerta)."""
    now = datetime.now(UTC)
    creditos = Decimal(str(payment.creditos)) if payment.creditos is not None else Decimal(0)
    if creditos <= 0 or sub.period_start is None:
        log.error("recarga_sin_acreditar", tenant_id=str(tid), pago=str(payment.id))
        return "sin_abono"
    ciclo, _ = ventana_mensual(sub.period_start, sub.dia_ancla, now)
    resultado = await session.execute(
        pg_insert(CreditGrant)
        .values(
            id=uuid.uuid4(),
            tenant_id=tid,
            ciclo=ciclo,
            tipo="recarga",
            creditos=creditos,
            vence_en=None,
            origen=str(payment.id),
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "ciclo", "tipo", "origen"])
    )
    return "acreditada" if resultado.rowcount else "ya_acreditada"


async def apply_payment(
    *,
    provider_reference: str,
    status: str,
    amount_in_cents: int | None,
    tenant_id: uuid.UUID | None = None,
) -> bool:
    """Aplica un evento de pago (idempotente). True si actualizó algo.

    Correlaciona por `provider_reference` (el id del link). Un pago ya `aprobado` o
    `duplicado` no se vuelve a tocar: ni un reintento del webhook ni un rechazo tardío de
    otro intento del mismo link lo deshacen. Solo se aplica un intento con link del panel
    (no el cobro de la renovación ni un pago del asistente). `amount_in_cents` es el monto
    FIRMADO por el proveedor: si falta o no es el del link, queda `monto_distinto` y se alerta
    (como en `pack_payments`).
    """
    if not provider_reference:
        return False

    async def _apply(tid: uuid.UUID) -> bool:
        from core.billing.service import (
            APLICADO,
            DUPLICADO,
            _suscripcion_bloqueada,
            aplicar_pago_de_periodo,
        )
        from core.events.outbox import emit_domain
        from core.shared.events import ON_PLATFORM_PERIOD_PAID

        duplicado = None
        anulado_pagado: str | None = None
        monto_distinto: tuple[str | None, int | None] | None = None
        periodo: Periodo | None = None
        recarga: str | None = None
        recarga_revertida: str | None = None
        monto_es_recarga = False
        async with tenant_scope(tid):
            async with get_session() as session:
                # Mismo orden de candados que el canal asistente (`aplicar_pago_de_periodo`):
                # primero la suscripción, después el pago. Al revés, los dos podían trabarse.
                sub_bloqueada = await _suscripcion_bloqueada(session, tid, datetime.now(UTC))
                payment = (
                    await session.execute(
                        select(SubscriptionPayment)
                        .where(
                            SubscriptionPayment.tenant_id == tid,
                            SubscriptionPayment.provider_reference == provider_reference,
                            SubscriptionPayment.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if payment is None or not payment.checkout_url or payment.provider in (PROVEEDOR_RENOVACION, "asistente"):
                    return False
                if payment.status in ("aprobado", DUPLICADO, "monto_distinto"):
                    if payment.status == "aprobado" and payment.concepto == "recarga" and status == "rechazado":
                        # Una recarga ya acreditada que el proveedor revierte después (VOIDED): el abono no se
                        # deshace (`credit_grants` es solo de inserción), se alerta para revisarlo a mano.
                        recarga_revertida = provider_reference
                    else:
                        return False  # ya procesado: idempotente
                elif status != "aprobado":
                    if status == "rechazado" and payment.status == "pendiente":
                        payment.status = "rechazado"
                        return True
                    return False
                if recarga_revertida is not None:
                    pass  # no se toca nada: se alerta fuera de la transacción
                elif amount_in_cents is None or int(amount_in_cents) != round(float(payment.amount_cop) * 100):
                    # Sin monto firmado o con otro monto: no se aplica (como `pack_payments`).
                    payment.status = "monto_distinto"
                    monto_distinto = (payment.periodo, amount_in_cents)
                    monto_es_recarga = payment.concepto == "recarga"
                    log.error(
                        "subscription_payment_monto_distinto",
                        tenant_id=str(tid),
                        esperado=str(payment.amount_cop),
                        recibido_centavos=amount_in_cents,
                    )
                elif payment.status == "anulado":
                    # Un intento que ya no servía (otro pago cubrió su período o el plan cambió)
                    # igual se pagó: la plata llegó de más. No se aplica (si no, un link viejo de
                    # Basic devolvería el negocio a Basic) y se alerta como pago doble.
                    payment.status = DUPLICADO
                    payment.paid_at = datetime.now(UTC)
                    anulado_pagado = payment.periodo or provider_reference
                elif payment.concepto == "recarga":
                    # Recarga de créditos (ADR-0209 §4): va por aquí ANTES de la rama de los pagos de plan
                    # sin período. Solo inserta su abono del ciclo actual; no toca período, plan ni estado.
                    payment.status = "aprobado"
                    payment.paid_at = datetime.now(UTC)
                    payment.updated_at = utcnow()
                    recarga = await _acreditar_recarga(session, tid, payment, sub_bloqueada)
                else:
                    if payment.periodo and payment.period_start and payment.period_end:
                        periodo = Periodo(payment.period_start, payment.period_end, payment.periodo)
                    else:
                        # Pago iniciado antes de ADR-0123: el período se calcula al aplicarlo.
                        sub = await _suscripcion(session, tid)
                        periodo = periodo_a_cobrar(
                            slug=await _slug(session, tid),
                            plan_id=sub.plan_id,
                            status=sub.status,
                            period_end=sub.period_end,
                            dia_ancla=sub.dia_ancla,
                            now=datetime.now(UTC),
                        )
                    resultado = await aplicar_pago_de_periodo(
                        session,
                        tid,
                        plan_id=payment.plan_id,
                        periodo=periodo,
                        provider=payment.provider,
                        provider_reference=provider_reference,
                        monto=payment.amount_cop,
                        plan_en_el_acto=True,
                    )
                    if resultado.resultado == APLICADO:
                        # Sincronización inversa por el outbox, en la MISMA transacción (ADR-0107).
                        await emit_domain(
                            session,
                            tenant_id=tid,
                            topic=ON_PLATFORM_PERIOD_PAID,
                            dedupe_key=f"plat_paid:{periodo.referencia}",
                            payload={
                                "referencia": periodo.referencia,
                                "periodo_inicio": periodo.inicio.isoformat(),
                                "periodo_fin": periodo.fin.isoformat(),
                                "plan_id": payment.plan_id,
                            },
                        )
                    elif resultado.resultado == DUPLICADO and resultado.nuevo:
                        duplicado = resultado
        if recarga_revertida is not None:
            from core.shared.alerts import send_alert

            log.error("recarga_revertida_tras_acreditar", tenant_id=str(tid))
            await send_alert(
                "Recarga revertida después de acreditarse",
                f"Negocio {tid}, pago {recarga_revertida}: el proveedor revirtió una recarga que ya se había "
                "acreditado. Los créditos no se descuentan solos: revisarlo a mano.",
                severity="critical",
            )
            return False
        if monto_distinto is not None:
            from core.shared.alerts import send_alert

            recibido = f"{monto_distinto[1]} centavos" if monto_distinto[1] is not None else "un monto sin firmar"
            que, cual = ("Recarga", f"pago {provider_reference}") if monto_es_recarga else (
                "Pago del plan", f"período {monto_distinto[0]}"
            )
            await send_alert(
                f"{que} con monto distinto",
                f"Negocio {tid}, {cual}: llegaron {recibido} y el link era por otro monto. "
                "No se aplicó: revisarlo a mano.",
                severity="critical",
            )
            return False
        if recarga is not None:
            if recarga == "sin_abono":
                from core.shared.alerts import send_alert

                await send_alert(
                    "Recarga pagada sin acreditar",
                    f"Negocio {tid}, pago {provider_reference}: la recarga se aprobó pero no se pudo insertar su "
                    "abono (sin ciclo o sin créditos). Revisarlo a mano.",
                    severity="critical",
                )
            log.info("recarga_aplicada", tenant_id=str(tid), resultado=recarga)
            return True
        if anulado_pagado is not None:
            from core.billing.canales import CANAL_PANEL, alertar_pago_duplicado, vinculo_asistente

            vinculo = await vinculo_asistente(tid)
            await alertar_pago_duplicado(
                tid, anulado_pagado, canal=CANAL_PANEL, cubierto_por="un intento anulado", vendedor=vinculo.vendedor if vinculo else None
            )
            return True
        if duplicado is not None:
            from core.billing.canales import CANAL_PANEL, alertar_pago_duplicado, vinculo_asistente

            vinculo = await vinculo_asistente(tid)
            await alertar_pago_duplicado(
                tid,
                duplicado.referencia,
                canal=CANAL_PANEL,
                cubierto_por=duplicado.cubierto_por,
                vendedor=vinculo.vendedor if vinculo else None,
            )
        log.info("subscription_payment_applied", tenant_id=str(tid), referencia=periodo.referencia if periodo else None)
        return True

    if tenant_id is not None:
        return await _apply(tenant_id)

    tid = await tenant_del_pago(provider_reference)
    if tid is None:
        return False
    return await _apply(tid)


async def tenant_del_pago(provider_reference: str) -> uuid.UUID | None:
    """Negocio de un pago de la plataforma por `provider_reference`, sin enumerar tenants:
    función SECURITY DEFINER que devuelve SOLO el `tenant_id`, nunca el contenido del pago.
    None si el link no es de un pago de la plataforma."""
    from sqlalchemy import text

    if not provider_reference:
        return None
    async with get_session() as session:
        return (
            await session.execute(
                text("select resolve_subscription_payment_tenant(:ref)"),
                {"ref": provider_reference},
            )
        ).scalar_one_or_none()
