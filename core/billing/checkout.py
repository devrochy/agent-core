"""Inicio y confirmación de la suscripción de un tenant a un plan pago (ADR-0046).

Es el **canal panel** del cobro de la plataforma (HU-SH-04, ADR-0123):

- `start_subscription(plan_id)`: el link de la plataforma para el período que toca pagar
  (`periodo_a_cobrar`), con su referencia `plat-<slug>-<AAAAMM>`. Si ese período ya está
  pagado (por el panel o por el asistente) responde «ya pagado» y no crea otro link.
- `apply_payment(provider_reference, status, ...)`: idempotente, invocado por el webhook de
  billing. Al aprobar, aplica el período con `aplicar_pago_de_periodo` (la misma puerta que
  usa el asistente) y avisa por el outbox para la sincronización inversa
  (`platform.period_paid`): el cargo del mismo período en el canal asistente queda pagado.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from core.billing.periodos import Periodo, periodo_a_cobrar, referencia_del_periodo
from core.billing.plans import get_plan
from core.billing.providers import get_billing_provider
from core.billing.providers.base import BillingRequestRejected
from core.config import get_settings
from core.db.models import SubscriptionPayment, Tenant, TenantSubscription, utcnow
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


async def apply_payment(
    *,
    provider_reference: str,
    status: str,
    tenant_id: uuid.UUID | None = None,
    amount_in_cents: int | None = None,
) -> bool:
    """Aplica un evento de pago (idempotente). True si actualizó algo.

    Correlaciona por `provider_reference` (el id del link). Un pago ya `aprobado` o
    `duplicado` no se vuelve a tocar: ni un reintento del webhook ni un rechazo tardío de
    otro intento del mismo link lo deshacen. Solo se aplica un intento con link del panel
    (no el cobro de la renovación ni un pago del asistente), y si el webhook trae el monto,
    tiene que ser el del link: si no, queda `monto_distinto` y se alerta (como en
    `pack_payments`).
    """
    if not provider_reference:
        return False

    async def _apply(tid: uuid.UUID) -> bool:
        from core.billing.service import APLICADO, DUPLICADO, aplicar_pago_de_periodo
        from core.events.outbox import emit_domain
        from core.shared.events import ON_PLATFORM_PERIOD_PAID

        duplicado = None
        monto_distinto: tuple[str | None, int] | None = None
        periodo: Periodo | None = None
        async with tenant_scope(tid):
            async with get_session() as session:
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
                    return False  # ya procesado: idempotente
                if status != "aprobado":
                    if status == "rechazado" and payment.status == "pendiente":
                        payment.status = "rechazado"
                        return True
                    return False
                if amount_in_cents is not None and int(amount_in_cents) != round(float(payment.amount_cop) * 100):
                    payment.status = "monto_distinto"
                    monto_distinto = (payment.periodo, int(amount_in_cents))
                    log.error(
                        "subscription_payment_monto_distinto",
                        tenant_id=str(tid),
                        esperado=str(payment.amount_cop),
                        recibido_centavos=int(amount_in_cents),
                    )
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
        if monto_distinto is not None:
            from core.shared.alerts import send_alert

            await send_alert(
                "Pago del plan con monto distinto",
                f"Negocio {tid}, período {monto_distinto[0]}: llegaron {monto_distinto[1]} centavos y el link "
                "era por otro monto. No se aplicó: revisarlo a mano.",
                severity="critical",
            )
            return False
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
