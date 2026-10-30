"""pack-subscriptions (ADR-0083, planes del catálogo desde SPEC-43): gestión, cobro del
período e inscripción manual por admin. Requiere stack local."""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from _subs_helpers import nueva_sub

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PLAN_BASIC = {"codigo": "PLAN-BASIC", "nombre": "Plan Basic", "precio": 200000}


async def _tenant_subs(packs: list[str] | None = None) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

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

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            sub = await nueva_sub(
                session,
                tenant_id,
                customer_id=customer_id,
                concepto=overrides.get("concepto", "Plan Basic"),
                estado=overrides.get("estado", "activa"),
                valor_periodo=overrides.get("valor_periodo", 200000),
                moneda="COP",
                **{k: v for k, v in overrides.items() if k in ("proximo_cargo", "fecha_alta")},
            )
            await session.flush()
            return sub.id


async def test_gestion_de_suscripcion_del_catalogo() -> None:
    """La suscripción apunta a un plan del catálogo y el proveedor del contrato (ADR-0106) la gestiona."""
    from decimal import Decimal

    from pack_subscriptions import planes
    from pack_subscriptions.capability import SubscriptionCapabilityImpl, crear_suscripcion_para

    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_subs()
    customer_id = uuid.uuid4()
    async with tenant_scope(tenant_id):
        plan = await planes.crear(
            planes.DatosDePlan(
                codigo=PLAN_BASIC["codigo"], nombre=PLAN_BASIC["nombre"], precio=Decimal(PLAN_BASIC["precio"]),
                periodicidad="mensual",
            )
        )
        sub = await crear_suscripcion_para(customer_id=customer_id, plan_id=plan.id)
        assert sub.concepto == "Plan Basic"
        assert float(sub.valor_periodo or 0) == 200000
        assert sub.estado == "activa"  # D-16: un plan pago no tiene prueba (HU-SUB-06)
        assert sub.trial_termina is None

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


async def test_crear_suscripcion_manual_admin() -> None:
    """Admin API: inscribe al cliente en un plan del catálogo (idempotente: repetir devuelve la misma)."""
    from decimal import Decimal

    from pack_subscriptions import planes
    from pack_subscriptions.admin_api import CrearSuscripcionBody, crear_suscripcion_manual
    from pack_subscriptions.models import Subscription
    from sqlalchemy import select

    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tenant_id = await _tenant_subs()
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            cliente = Customer(
                tenant_id=tenant_id, channel="whatsapp", external_id=f"whatsapp:{uniq('c')}", name="Ana",
                contact_phone="+573001112233",
            )
            session.add(cliente)
            await session.flush()
            customer_id = cliente.id
        await planes.crear(
            planes.DatosDePlan(
                codigo="PLAN-EMPRESARIAL", nombre="Plan Empresarial", precio=Decimal(1800000), periodicidad="mensual"
            )
        )
    body = CrearSuscripcionBody(customer_id=customer_id, plan_codigo="PLAN-EMPRESARIAL")

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
        assert out1.estado == "pendiente_pago" and out1.numero_publico
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
