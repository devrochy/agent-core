"""pack-subscriptions (ADR-0083, planes como productos): gestión, cobro del
período, activación por pago de pedido y creación manual por admin. Requiere stack local."""

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

PLAN_SNAPSHOT = {"nombre": "Plan Basic", "precio": 200000, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 30}


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


async def _crear_suscripcion_directa(tenant_id, customer_id, **overrides) -> uuid.UUID:
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            sub = Subscription(
                tenant_id=tenant_id,
                customer_id=customer_id,
                concepto=overrides.get("concepto", "Plan Basic"),
                estado=overrides.get("estado", "activa"),
                valor_periodo=overrides.get("valor_periodo", 200000),
                moneda="COP",
                **{k: v for k, v in overrides.items() if k in ("proximo_cargo", "fecha_alta")},
            )
            session.add(sub)
            await session.flush()
            return sub.id


async def test_gestion_de_suscripcion_por_snapshot() -> None:
    """La suscripción se crea con el snapshot (sin tabla Plan) y el proveedor
    del contrato v1 (ADR-0106) la gestiona."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.module import crear_suscripcion_para

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_subs()
    customer_id = uuid.uuid4()
    async with tenant_scope(tenant_id):
        sub = await crear_suscripcion_para(
            customer_id=customer_id, variante_id=uuid.uuid4(), plan=PLAN_SNAPSHOT
        )
        assert sub.concepto == "Plan Basic"
        assert float(sub.valor_periodo or 0) == 200000
        assert sub.estado == "activa"  # D-16: un plan pago no tiene prueba (HU-SUB-06)
        assert sub.trial_termina is None
        # Idempotente: no crea una segunda activa.
        sub2 = await crear_suscripcion_para(customer_id=customer_id, variante_id=None, plan=PLAN_SNAPSHOT)
        assert sub2.id == sub.id

        cap = SubscriptionCapabilityImpl()
        vistas = await cap.list_for_customer(customer_id)
        assert vistas[0]["plan"] == "Plan Basic"
        assert vistas[0]["estado"] == "activa"

        pausada = await cap.pause(sub.id, actor="test")
        assert pausada["estado"] == "pausada"
        renovada = await cap.resume(sub.id, actor="test")
        assert renovada["estado"] == "activa"


async def test_link_pago_genera_cargo_y_webhook_lo_paga() -> None:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.models import SubscriptionCharge
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.subscriptions import SubscriptionError

    tenant_id = await _tenant_subs()  # sin pack_payments → sin_pasarela
    customer_id = uuid.uuid4()
    sub_id = await _crear_suscripcion_directa(tenant_id, customer_id)

    cap = SubscriptionCapabilityImpl()
    async with tenant_scope(tenant_id):
        with pytest.raises(SubscriptionError) as exc:
            await cap.charge_link(sub_id)
        assert exc.value.code == "sin_pasarela"

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
            assert cargos[0].subscription_id == sub_id

            from pack_subscriptions.module import SubscriptionsModule

            await SubscriptionsModule()._on_payment_confirmed(f"sub-{cargos[0].id}", "wompi", "ref-1", 200000.0)

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


async def test_cron_genera_cargo_y_avanza_periodo() -> None:
    """#156: el cron crea el cargo del período y avanza `proximo_cargo` (idempotente)."""
    from datetime import UTC, datetime, timedelta

    from pack_subscriptions.jobs import generar_cargos
    from pack_subscriptions.models import SubscriptionCharge
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_subs()
    customer_id = uuid.uuid4()
    vencido = datetime.now(UTC) - timedelta(days=1)
    sub_id = await _crear_suscripcion_directa(
        tenant_id, customer_id, proximo_cargo=vencido, fecha_alta=vencido - timedelta(days=30)
    )

    await generar_cargos()

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            cargos = (
                await session.execute(
                    select(SubscriptionCharge).where(
                        SubscriptionCharge.tenant_id == tenant_id,
                        SubscriptionCharge.subscription_id == sub_id,
                        SubscriptionCharge.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
            assert len(cargos) == 1
            assert float(cargos[0].monto or 0) == 200000
            assert cargos[0].estado == "pendiente"
            assert cargos[0].periodo == vencido.strftime("%Y-%m")

    # Idempotente: una segunda corrida no duplica el cargo del período ya vencido.
    await generar_cargos()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            n = (
                await session.execute(
                    select(SubscriptionCharge).where(
                        SubscriptionCharge.tenant_id == tenant_id,
                        SubscriptionCharge.subscription_id == sub_id,
                        SubscriptionCharge.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
            assert len(n) == 1


async def test_opcion_b_commerce_activa_suscripcion_por_pago() -> None:
    """El plan es un producto de commerce (billing_period); al pagarse el pedido,
    commerce entrega el snapshot y el registry activa la suscripción."""
    from pack_commerce.models import Order, OrderItem, Product, ProductVariant
    from pack_subscriptions.models import Subscription
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_subs(["pack_commerce", "pack_subscriptions"])
    customer_id = uuid.uuid4()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            product = Product(tenant_id=tenant_id, name="Plan Basic", description="Recurrente", has_variants=False)
            session.add(product)
            await session.flush()
            variant = ProductVariant(
                tenant_id=tenant_id,
                product_id=product.id,
                sku="PLAN-BASIC",
                price=200000,
                billing_period="mensual",
                trial_days=30,
            )
            session.add(variant)
            await session.flush()
            variant_id = variant.id

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
                public_order_number="OC-0001",
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
            assert float(subs[0].valor_periodo or 0) == 200000
            # D-16 (HU-SUB-06): el trial_days=30 de una variante paga se ignora.
            assert subs[0].estado == "activa"
            assert subs[0].trial_termina is None


async def test_crear_suscripcion_manual_admin() -> None:
    """Admin API: crea la suscripción manualmente a partir de un snapshot (idempotente)."""
    from pack_subscriptions.admin_api import CrearSuscripcionBody, crear_suscripcion_manual
    from pack_subscriptions.models import Subscription
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_subs()
    customer_id = uuid.uuid4()
    body = CrearSuscripcionBody(
        customer_id=customer_id,
        variant_id=uuid.uuid4(),
        nombre="Plan Empresarial",
        precio=1800000,
        moneda="COP",
        periodicidad="mensual",
        trial_dias=0,
    )

    # El handler usa `require(...)`: lo invocamos con un principal del tenant.
    from core.api.deps import Principal
    from core.db.models import Admin

    admin = Admin(
        id=uuid.uuid4(), email="admin@solvershub.test", username=None,
        password_hash="x", role="master", active=True,
    )
    async with tenant_scope(tenant_id):
        principal = Principal(admin=admin, tenant_id=tenant_id, tenants=[], permissions={"pack_subscriptions:suscripciones": None})
        out1 = await crear_suscripcion_manual(body, principal)
        assert out1.creada is True
        out2 = await crear_suscripcion_manual(body, principal)
        assert out2.suscripcion_id == out1.suscripcion_id  # idempotente

        async with get_session() as session:
            subs = (
                await session.execute(
                    select(Subscription).where(
                        Subscription.tenant_id == tenant_id,
                        Subscription.customer_id == customer_id,
                    )
                )
            ).scalars().all()
            assert len(subs) == 1
            assert subs[0].concepto == "Plan Empresarial"
            assert float(subs[0].valor_periodo or 0) == 1800000