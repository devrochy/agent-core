"""Cron jobs de pack-subscriptions (ADR-0083, #156).

- `generar_cargos`: genera el cargo del período de cada suscripción vencida y,
  si `pack_payments` está habilitado, su link de pago. Idempotente por período.

El decorador `@register_cron` salta si el pack está inactivo; el filtro por
tenant habilitado es explícito (`_tenant_ids`, ADR-0019).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, text

from core.db.session import get_session
from core.events.outbox import emit_domain
from core.shared.events import ON_SUBSCRIPTION_CHARGE_CREATED, ON_SUBSCRIPTION_PAST_DUE
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.decorators import register_cron
from pack_subscriptions.models import Subscription, SubscriptionCharge
from pack_subscriptions.periodos import etiqueta_periodo, sumar_periodo

log = get_logger(__name__)

PACK_ID = "pack_subscriptions"


async def _tenant_ids() -> list[uuid.UUID]:
    """Tenants cuyo AgentSpec incluye este pack (ADR-0019)."""
    from core.agents.spec import enabled_packs, load_active_spec

    async with get_session() as session:
        rows = (
            await session.execute(text("select id from tenants where deleted_at is null"))
        ).scalars().all()

    result: list[uuid.UUID] = []
    for raw in rows:
        tid = uuid.UUID(str(raw))
        try:
            async with tenant_scope(tid):
                spec = await load_active_spec()
            if PACK_ID in enabled_packs(spec):
                result.append(tid)
        except Exception:
            continue
    return result


async def _config_del_negocio():  # type: ignore[no-untyped-def]
    from core.plugin_manager import plugin_manager
    from pack_subscriptions.config import SubscriptionConfig

    try:
        config = await plugin_manager.load_config(PACK_ID)
    except Exception:
        config = None
    return config if isinstance(config, SubscriptionConfig) else SubscriptionConfig()


@register_cron(PACK_ID, "15 3 * * *")
async def generar_cargos() -> None:
    await _generar_cargos(datetime.now(UTC))


async def _generar_cargos(now: datetime) -> int:
    """Cargo del período de cada suscripción que llegó a su `proximo_cargo`
    (ADR-0083, HU-SUB-04). Idempotente por período.

    - El período sale del calendario (`periodicidad` + `dia_ancla`), no `+30 días`.
    - No cambia el estado: antes ponía `activa` aunque nadie pagara (defecto 2).
    - No cobra durante una prueba ni planes en $0 (Free, HU-SUB-06).
    - Las `vencida` no acumulan cargos: al pagar, el período siguiente se calcula
      desde el pagado (`_on_payment_confirmed`).
    Si hay pasarela, genera el link (`sub-<cargo.id>`) y avisa al cliente por el outbox.
    """
    total = 0
    for tenant_id in await _tenant_ids():
        from core.agents.spec import enabled_packs, load_active_spec
        from core.payments import payment_registry

        async with tenant_scope(tenant_id):
            spec = await load_active_spec()
            cap = payment_registry.capability_for(enabled_packs(spec))
            async with get_session() as session:
                subs = (
                    await session.execute(
                        select(Subscription).where(
                            Subscription.tenant_id == tenant_id,
                            Subscription.estado.in_(("trialing", "activa")),
                            Subscription.deleted_at.is_(None),
                            Subscription.proximo_cargo.isnot(None),
                            Subscription.proximo_cargo <= now,
                        )
                    )
                ).scalars().all()
                generadas = 0
                for sub in subs:
                    if sub.trial_termina is not None and sub.trial_termina > now:
                        continue
                    inicio = sub.proximo_cargo
                    siguiente = sumar_periodo(inicio, sub.periodicidad, sub.dia_ancla)
                    if not Decimal(sub.valor_periodo or 0):
                        sub.periodo_actual_inicio = inicio
                        sub.proximo_cargo = siguiente
                        continue
                    periodo = etiqueta_periodo(inicio, sub.periodicidad)
                    existe = (
                        await session.execute(
                            select(SubscriptionCharge).where(
                                SubscriptionCharge.tenant_id == tenant_id,
                                SubscriptionCharge.subscription_id == sub.id,
                                SubscriptionCharge.periodo == periodo,
                                SubscriptionCharge.deleted_at.is_(None),
                            )
                        )
                    ).scalars().first()
                    sub.periodo_actual_inicio = inicio
                    sub.proximo_cargo = siguiente
                    if existe is not None:
                        continue
                    cargo = SubscriptionCharge(
                        tenant_id=tenant_id,
                        subscription_id=sub.id,
                        periodo=periodo,
                        monto=Decimal(sub.valor_periodo or 0),
                        estado="pendiente",
                        fecha_vencimiento=inicio,
                    )
                    session.add(cargo)
                    await session.flush()
                    generadas += 1
                    link_url: str | None = None
                    if cap is not None:
                        try:
                            link = await cap.create_link(
                                reference=f"sub-{cargo.id}",
                                amount=float(cargo.monto or 0),
                                description=f"Suscripción {sub.concepto}",
                            )
                            cargo.referencia = link.reference
                            link_url = link.url
                        except Exception:
                            log.warning("subs_link_fallido", subscription_id=str(sub.id), exc_info=True)
                    # Aviso al cliente por el outbox, en la MISMA transacción que el
                    # cargo (ADR-0107): si el cargo se revierte, no hay aviso. El core
                    # lo envía como plantilla o texto según la ventana (ADR-0108).
                    await emit_domain(
                        session,
                        tenant_id=tenant_id,
                        topic=ON_SUBSCRIPTION_CHARGE_CREATED,
                        dedupe_key=f"sub_charge:{cargo.id}",
                        payload={
                            "charge_id": str(cargo.id),
                            "subscription_id": str(sub.id),
                            "customer_id": str(sub.customer_id),
                            "concepto": sub.concepto,
                            "monto": str(cargo.monto),
                            "moneda": sub.moneda,
                            "periodo": periodo,
                            "fecha_vencimiento": inicio.date().isoformat(),
                            "referencia": cargo.referencia,
                            "link": link_url,
                        },
                    )
        total += generadas
        log.info("subs_cargos_generados", tenant_id=str(tenant_id), generadas=generadas)
    return total


@register_cron(PACK_ID, "45 3 * * *")
async def marcar_vencidas() -> None:
    await _marcar_vencidas(datetime.now(UTC))


async def _marcar_vencidas(now: datetime) -> int:
    """Pasa a `vencida` la suscripción con un cargo impago más allá de los días de
    gracia del negocio (D-SPEC-06-3, `SubscriptionConfig.dias_gracia`) y emite
    `subscription.past_due` por el outbox (HU-SUB-04; los avisos son HU-SUB-05)."""
    total = 0
    for tenant_id in await _tenant_ids():
        async with tenant_scope(tenant_id):
            config = await _config_del_negocio()
            limite = now - timedelta(days=config.dias_gracia)
            async with get_session() as session:
                filas = (
                    await session.execute(
                        select(SubscriptionCharge, Subscription)
                        .join(Subscription, Subscription.id == SubscriptionCharge.subscription_id)
                        .where(
                            SubscriptionCharge.tenant_id == tenant_id,
                            Subscription.tenant_id == tenant_id,
                            SubscriptionCharge.estado == "pendiente",
                            SubscriptionCharge.deleted_at.is_(None),
                            SubscriptionCharge.fecha_vencimiento.isnot(None),
                            SubscriptionCharge.fecha_vencimiento <= limite,
                            Subscription.estado.in_(("activa", "trialing")),
                            Subscription.deleted_at.is_(None),
                        )
                        .order_by(SubscriptionCharge.fecha_vencimiento)
                    )
                ).all()
                vistas: set[uuid.UUID] = set()
                for cargo, sub in filas:
                    if sub.id in vistas:
                        continue
                    vistas.add(sub.id)
                    sub.estado = "vencida"
                    await emit_domain(
                        session,
                        tenant_id=tenant_id,
                        topic=ON_SUBSCRIPTION_PAST_DUE,
                        dedupe_key=f"sub_past_due:{cargo.id}",
                        payload={
                            "charge_id": str(cargo.id),
                            "subscription_id": str(sub.id),
                            "customer_id": str(sub.customer_id),
                            "concepto": sub.concepto,
                            "monto": str(cargo.monto),
                            "moneda": sub.moneda,
                            "periodo": cargo.periodo,
                            "fecha_vencimiento": cargo.fecha_vencimiento.date().isoformat(),
                            "dias_gracia": config.dias_gracia,
                        },
                    )
            total += len(vistas)
            if vistas:
                log.info("subs_vencidas", tenant_id=str(tenant_id), total=len(vistas))
    return total
