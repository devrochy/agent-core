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
from core.shared.events import ON_SUBSCRIPTION_CHARGE_CREATED
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.decorators import register_cron
from pack_subscriptions.models import Subscription, SubscriptionCharge

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


def _proximo(desde: datetime) -> datetime:
    return (desde + timedelta(days=30)).replace(second=0, microsecond=0)


@register_cron(PACK_ID, "15 3 * * *")
async def generar_cargos() -> None:
    """Cargo del período de cada suscripción vencida (ADR-0083, #156).

    Crea un `SubscriptionCharge` pendiente por el valor del período y avanza
    `proximo_cargo` al siguiente ciclo. Si hay pasarela (`pack_payments`
    habilitado), genera además el link de pago (referencia `sub-<cargo.id>`,
    que `payment.confirmed` marca pagado). Idempotente por período.
    """
    now = datetime.now(UTC)
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
                for sub in subs:
                    periodo = sub.proximo_cargo.strftime("%Y-%m")
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
                    if existe is not None:
                        continue
                    cargo = SubscriptionCharge(
                        tenant_id=tenant_id,
                        subscription_id=sub.id,
                        periodo=periodo,
                        monto=Decimal(sub.valor_periodo or 0),
                        estado="pendiente",
                        fecha_vencimiento=sub.proximo_cargo,
                    )
                    session.add(cargo)
                    await session.flush()
                    sub.estado = "activa"
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
                            "fecha_vencimiento": cargo.fecha_vencimiento.date().isoformat()
                            if cargo.fecha_vencimiento
                            else None,
                            "referencia": cargo.referencia,
                            "link": link_url,
                        },
                    )
                    sub.proximo_cargo = _proximo(sub.proximo_cargo)
        log.info("subs_cargos_generados", tenant_id=str(tenant_id), generadas=len(subs))