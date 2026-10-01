"""pack-subscriptions (ADR-0083, Opción B): seed de planes, tools sub_*, cobro
del período y activación desde commerce. Requiere stack local."""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PLANES = {
    "planes": [
        {"nombre": "Plan Free", "precio": 0, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 0},
        {"nombre": "Plan Basic", "precio": 200000, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 30},
        {"nombre": "Plan Premium", "precio": 600000, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 30},
    ]
}


async def _tenant_subs(packs: list[str] | None = None) -> uuid.UUID:
    from core.onboarding import onboard_tenant

    slug = uniq("sub")
    tenant_id, _ = await onboard_tenant(
        name=f"Suscripción {slug}",
        slug=slug,
        business_description="Planes recurrentes de prueba.",
        persona={"tone": "cercano"},
        packs=packs or ["pack_subscriptions"],
    )
    return tenant_id


async def test_seed_planes_idempotente() -> None:
    from pack_subscriptions.models import Plan
    from pack_subscriptions.module import SubscriptionsModule
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_subs()
    module = SubscriptionsModule()
    first = await module.seed_data(tenant_id, PLANES)
    second = await module.seed_data(tenant_id, PLANES)
    assert len(first) == 3
    assert second == []
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            rows = (
                await session.execute(
                    select(Plan).where(Plan.tenant_id == tenant_id, Plan.deleted_at.is_(None))
                )
            ).scalars().all()
            assert len(rows) == 3
            precios = {p.nombre: float(p.precio or 0) for p in rows}
            assert precios["Plan Basic"] == 200000


async def test_tools_plan_y_suscripcion() -> None:
    from pack_subscriptions.models import Plan
    from pack_subscriptions.module import SubscriptionsModule
    from pack_subscriptions.skills import SubscriptionsSkill
    from sqlalchemy import select

    from core.db.session import get_session
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_subs()
    await SubscriptionsModule().seed_data(tenant_id, PLANES)

    customer_id = uuid.uuid4()
    ctx = ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=customer_id,
        idempotency_key=str(uuid.uuid4()),
        channel="test",
    )
    skill = SubscriptionsSkill()
    async with tenant_scope(tenant_id):
        planes = await skill.informar_planes({}, ctx)
        assert planes["total"] == 3
        basic = next(p for p in planes["planes"] if p["nombre"] == "Plan Basic")
        assert basic["precio"] == 200000

        creada = await skill.crear_suscripcion({"plan_id": basic["plan_id"]}, ctx)
        assert creada.get("creada") is True
        consulta = await skill.consultar_suscripcion({}, ctx)
        assert consulta["encontrado"] is True
        assert consulta["plan"] == "Plan Basic"
        assert consulta["estado"] == "trialing"

        # Idempotente: no crea una segunda activa.
        otra = await skill.crear_suscripcion({"plan_id": basic["plan_id"]}, ctx)
        assert otra.get("ya_existe") is True

        pausada = await skill.pausar({}, ctx)
        assert pausada["estado"] == "pausada"
        renovada = await skill.renovar({}, ctx)
        assert renovada["estado"] == "activa"

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            sub_id = (await session.execute(select(Plan))).scalars().first()  # sanity: tabla existe
            assert sub_id is not None


async def test_link_pago_genera_cargo_y_webhook_lo_paga() -> None:
    from pack_subscriptions.models import SubscriptionCharge
    from pack_subscriptions.module import SubscriptionsModule
    from pack_subscriptions.skills import SubscriptionsSkill
    from sqlalchemy import select

    from core.db.session import get_session
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_subs()  # sin pack_payments → sin_pasarela
    await SubscriptionsModule().seed_data(tenant_id, PLANES)

    customer_id = uuid.uuid4()
    ctx = ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=customer_id,
        idempotency_key=str(uuid.uuid4()),
        channel="test",
    )
    skill = SubscriptionsSkill()
    async with tenant_scope(tenant_id):
        planes = await skill.informar_planes({}, ctx)
        basic = next(p for p in planes["planes"] if p["nombre"] == "Plan Basic")
        await skill.crear_suscripcion({"plan_id": basic["plan_id"]}, ctx)

        link = await skill.link_pago({}, ctx)
        assert link.get("sin_pasarela") is True  # no pack_payments en este tenant
        assert link["encontrado"] is True

        async with get_session() as session:
            cargos = (
                await session.execute(
                    select(SubscriptionCharge).where(
                        SubscriptionCharge.tenant_id == tenant_id,
                        SubscriptionCharge.estado == "pendiente",
                    )
                )
            ).scalars().all()
            assert len(cargos) == 1
            assert float(cargos[0].monto or 0) == 200000

            # El pack escucha payment.confirmed: referencia `sub-<cargo.id>`.
            from pack_subscriptions.module import SubscriptionsModule

            module = SubscriptionsModule()
            await module._on_payment_confirmed(f"sub-{cargos[0].id}", "wompi", "ref-1", 200000.0)

        async with tenant_scope(tenant_id):
            async with get_session() as session:
                cargo = (
                    await session.execute(
                        select(SubscriptionCharge).where(
                            SubscriptionCharge.tenant_id == tenant_id,
                            SubscriptionCharge.id == cargos[0].id,
                        )
                    )
                ).scalar_one()
                assert cargo.estado == "pagado"


async def test_opcion_b_commerce_activa_suscripcion() -> None:
    """Commerce vende el plan; al pagarse el pedido, el registry activa la
    suscripción del Customer (invariante nº5: sin imports entre packs)."""
    from pack_commerce.models import Order, OrderItem
    from pack_subscriptions.models import Plan, Subscription
    from pack_subscriptions.module import SubscriptionsModule
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_subs(["pack_commerce", "pack_subscriptions"])

    # Plan vinculado a una variante (como haría el seed con variante_sku).
    module = SubscriptionsModule()
    data = {"planes": [{"nombre": "Plan Basic", "precio": 200000, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 30, "variante_id": str(uuid.uuid4())}]}
    await module.seed_data(tenant_id, data)

    customer_id = uuid.uuid4()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            plan = (
                await session.execute(
                    select(Plan).where(Plan.tenant_id == tenant_id, Plan.nombre == "Plan Basic")
                )
            ).scalars().first()
            variant_id = plan.variante_id

    # Pedido "pagado" con el plan como item.
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            order = Order(
                tenant_id=tenant_id,
                conversation_id=uuid.uuid4(),
                customer_id=customer_id,
                status="abierto",
                payment_status="pagado",
                total=200000,
                idempotency_key=str(uuid.uuid4()),
                order_seq=1,
            )
            session.add(order)
            await session.flush()
            session.add(
                OrderItem(
                    tenant_id=tenant_id,
                    order_id=order.id,
                    variant_id=variant_id,
                    quantity=1,
                    unit_price=200000,
                )
            )
            await session.flush()

            from pack_commerce.module import CRMModule

            await CRMModule()._activate_plans_for_order(session, order)

            subs = (
                await session.execute(
                    select(Subscription).where(Subscription.tenant_id == tenant_id)
                )
            ).scalars().all()
            assert len(subs) == 1
            assert subs[0].customer_id == customer_id
            assert subs[0].concepto == "Plan Basic"
            assert subs[0].estado == "trialing"