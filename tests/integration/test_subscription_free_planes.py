"""HU-SUB-06 · Free con prueba y planes pagos sin prueba (D-16), contra la BD real.

Gherkin: Free sin cobro, fin de la prueba (oferta y vencida), planes pagos (prueba
ignorada y avisada, cambio desde Free) y contra entrega que no nace pagado.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _api_helpers import uniq
from conftest import TEST_ALPHA_ID
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

AHORA = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
FREE = {"nombre": "Free", "precio": 0, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 30}
BASIC = {"nombre": "Basic", "precio": 99000, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 30}


async def _negocio(packs: list[str] | None = None) -> uuid.UUID:
    from core.onboarding import onboard_tenant

    slug = uniq("free")
    tid, _ = await onboard_tenant(
        name=f"Negocio {slug}", slug=slug, packs=packs or ["pack_subscriptions"]
    )
    return tid


async def _eventos(tid, topic: str) -> list:
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list(
                (
                    await s.execute(
                        select(DomainEvent).where(DomainEvent.tenant_id == tid, DomainEvent.topic == topic)
                    )
                ).scalars().all()
            )


async def _sub_free(tid, *, trial_termina: datetime) -> uuid.UUID:
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            sub = Subscription(
                tenant_id=tid, customer_id=uuid.uuid4(), concepto="Free", estado="trialing",
                valor_periodo=Decimal("0"), moneda="COP", periodicidad="mensual", dia_ancla=21,
                trial_termina=trial_termina, proximo_cargo=trial_termina,
                fecha_alta=trial_termina - timedelta(days=30),
            )
            s.add(sub)
            await s.flush()
            return sub.id


async def _leer(tid, id_):
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return await s.get(Subscription, id_)


async def test_free_nace_en_prueba_y_emite_activated() -> None:
    """Gherkin «Free sin cobro»: 30 días de prueba, sin cargo, `subscription.activated`."""
    from pack_subscriptions.capability import crear_suscripcion_para

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    antes = datetime.now(UTC)
    async with tenant_scope(tid):
        sub = await crear_suscripcion_para(customer_id=uuid.uuid4(), variante_id=uuid.uuid4(), plan=FREE)
    assert sub.estado == "trialing"
    assert Decimal(sub.valor_periodo) == 0
    assert sub.trial_termina is not None
    assert timedelta(days=29, hours=23) < sub.trial_termina - antes < timedelta(days=30, minutes=5)
    eventos = await _eventos(tid, "subscription.activated")
    assert [e.payload["subscription_id"] for e in eventos] == [str(sub.id)]
    assert eventos[0].payload["estado"] == "trialing"


async def test_plan_pago_ignora_la_prueba() -> None:
    """Gherkin «Planes pagos»: Basic nace `activa` aunque el snapshot traiga prueba."""
    from pack_subscriptions.capability import crear_suscripcion_para

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        sub = await crear_suscripcion_para(customer_id=uuid.uuid4(), variante_id=uuid.uuid4(), plan=BASIC)
    assert sub.estado == "activa"
    assert sub.trial_termina is None
    assert sub.proximo_cargo is not None


async def test_variante_paga_con_trial_queda_en_audit() -> None:
    """El `trial_days` de una variante paga se ignora y queda registrado para el panel."""
    from pack_commerce.models import Order, OrderItem, Product, ProductVariant
    from pack_commerce.planes import activar_planes_del_pedido
    from pack_subscriptions.models import Subscription

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio(["pack_commerce", "pack_subscriptions"])
    customer_id = uuid.uuid4()
    async with tenant_scope(tid):
        async with get_session() as s:
            product = Product(tenant_id=tid, name="Basic", description="Plan", has_variants=False)
            s.add(product)
            await s.flush()
            variant = ProductVariant(
                tenant_id=tid, product_id=product.id, sku="PLAN-BASIC", price=99000,
                billing_period="mensual", trial_days=30,
            )
            s.add(variant)
            await s.flush()
            order = Order(
                tenant_id=tid, conversation_id=uuid.uuid4(), customer_id=customer_id,
                status="abierto", payment_status="pagado", total=99000,
                idempotency_key=str(uuid.uuid4()), order_seq=1, public_order_number="OC-0001",
            )
            s.add(order)
            await s.flush()
            s.add(OrderItem(tenant_id=tid, order_id=order.id, variant_id=variant.id, quantity=1, unit_price=99000))
            await s.flush()
            assert await activar_planes_del_pedido(s, order) == 1

        async with get_session() as s:
            sub = (
                await s.execute(select(Subscription).where(Subscription.customer_id == customer_id))
            ).scalar_one()
            audit = (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tid, AuditLog.action == "trial_ignorado_en_plan_pago"
                    )
                )
            ).scalars().all()
    assert sub.estado == "activa"
    assert sub.trial_termina is None
    assert len(audit) == 1
    assert audit[0].output["sku"] == "PLAN-BASIC"


async def test_free_pasa_a_basic_en_la_misma_suscripcion() -> None:
    """Comprar Basic estando en Free cambia el plan (antes devolvía la Free sin tocar)."""
    from pack_subscriptions.capability import crear_suscripcion_para

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    customer_id = uuid.uuid4()
    async with tenant_scope(tid):
        free = await crear_suscripcion_para(customer_id=customer_id, variante_id=uuid.uuid4(), plan=FREE)
        # Repetir Free es idempotente.
        otra = await crear_suscripcion_para(customer_id=customer_id, variante_id=free.variante_id, plan=FREE)
        assert otra.id == free.id
        basic = await crear_suscripcion_para(customer_id=customer_id, variante_id=uuid.uuid4(), plan=BASIC)
    assert basic.id == free.id
    assert basic.estado == "activa"
    assert basic.concepto == "Basic"
    assert Decimal(basic.valor_periodo) == Decimal("99000")
    assert basic.trial_termina is None
    eventos = await _eventos(tid, "subscription.activated")
    assert len(eventos) == 2
    assert sorted(e.payload["cambio_de_plan"] for e in eventos) == [False, True]


async def test_oferta_antes_del_fin_de_la_prueba_una_sola_vez() -> None:
    """Gherkin «Fin de la prueba»: oferta 3 días antes, sin repetir."""
    from pack_subscriptions.jobs import _avisos_de_cobro

    tid = await _negocio()
    cerca = await _sub_free(tid, trial_termina=AHORA + timedelta(days=2))
    await _sub_free(tid, trial_termina=AHORA + timedelta(days=10))

    await _avisos_de_cobro(AHORA)
    await _avisos_de_cobro(AHORA + timedelta(days=1))
    eventos = await _eventos(tid, "subscription.trial_ending")
    assert [e.payload["subscription_id"] for e in eventos] == [str(cerca)]
    assert eventos[0].dedupe_key == f"sub_trial_ending:{cerca}"
    assert "{prueba.termina}" in eventos[0].payload["texto"]


async def test_free_vence_al_terminar_la_prueba_sin_cargo() -> None:
    """Gherkin «Fin de la prueba»: al día 30 sin pasar a un plan pago → `vencida`."""
    from pack_subscriptions.jobs import _generar_cargos, _marcar_vencidas
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    terminada = await _sub_free(tid, trial_termina=AHORA - timedelta(hours=1))
    vigente = await _sub_free(tid, trial_termina=AHORA + timedelta(days=5))

    await _generar_cargos(AHORA)
    await _marcar_vencidas(AHORA)
    sub = await _leer(tid, terminada)
    assert sub.estado == "vencida"
    assert sub.proximo_cargo is None
    assert (await _leer(tid, vigente)).estado == "trialing"
    async with tenant_scope(tid):
        async with get_session() as s:
            cargos = (await s.execute(select(SubscriptionCharge))).scalars().all()
    assert cargos == []
    eventos = await _eventos(tid, "subscription.past_due")
    assert [e.dedupe_key for e in eventos] == [f"sub_trial_expired:{terminada}"]
    assert eventos[0].payload["motivo"] == "fin_de_prueba"


# --------------------------------------------------------------------- commerce


async def _pedido_de_plan(precio: float) -> dict:
    from pack_commerce.models import Product, ProductVariant, Quote, QuoteItem
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    t1 = TEST_ALPHA_ID
    async with tenant_scope(t1):
        async with get_session() as s:
            sku = f"PLAN-{uuid.uuid4().hex[:6]}"
            product = Product(tenant_id=t1, name=f"Plan {sku}")
            s.add(product)
            await s.flush()
            variant = ProductVariant(
                tenant_id=t1, product_id=product.id, sku=sku, price=precio, billing_period="mensual"
            )
            s.add(variant)
            await s.flush()
            quote = Quote(
                tenant_id=t1, conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(),
                subtotal=precio, discount=0, total=precio, status="aceptada",
            )
            s.add(quote)
            await s.flush()
            s.add(QuoteItem(tenant_id=t1, quote_id=quote.id, variant_id=variant.id, quantity=1, unit_price=precio))
            await s.flush()
            quote_id = str(quote.id)

    ctx = ToolContext(
        conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(),
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}", channel="whatsapp",
    )
    async with tenant_scope(t1):
        return await OrderSkill().crear_pedido(
            {
                "quote_id": quote_id,
                "payment_method": "efectivo_contraentrega",
                "delivery_method": "domicilio",
                "customer_data": {
                    "full_name": "Test", "id_document": "1", "address": "Calle 1",
                    "municipality": "Manizales", "city": "Manizales",
                },
            },
            ctx,
        )


async def _estado_pago(order_id: str) -> str:
    from pack_commerce.models import Order

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(TEST_ALPHA_ID):
        async with get_session() as s:
            order = await s.get(Order, uuid.UUID(order_id))
            return order.payment_status


async def test_plan_contra_entrega_no_nace_pagado() -> None:
    """Gherkin «Contra entrega»: el plan espera el pago real (antes nacía `pagado`)."""
    result = await _pedido_de_plan(99000.0)
    assert result.get("status") == "confirmed", result
    assert await _estado_pago(result["order_id"]) == "pendiente"


async def test_pedido_free_nace_pagado() -> None:
    """Un pedido de Free no tiene nada que cobrar: nace pagado (D-16)."""
    result = await _pedido_de_plan(0.0)
    assert result.get("status") == "confirmed", result
    assert await _estado_pago(result["order_id"]) == "pagado"
