"""HU-SUB-09 · comercio agnóstico al proveedor de suscripciones, contra la BD real.

Cobranza como proveedor llega en HU-SUB-15: hasta entonces un proveedor de prueba
se registra con su id. Gherkin: elección por configuración, ambigüedad (pedido
pagado, activación pendiente, aviso y reintento) y sin proveedor (el panel impide
crear el producto recurrente).
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


class _ProveedorDePrueba:
    provider_id = "pack_cobranza"

    def __init__(self) -> None:
        self.activaciones: list[dict] = []

    async def activate(self, *, customer_id, variant_id, plan, reference=""):
        self.activaciones.append({"customer_id": customer_id, "plan": plan, "reference": reference})
        return {"subscription_id": str(uuid.uuid4()), "estado": "activa", "plan": plan.get("nombre")}


@pytest.fixture()
def proveedor_cobranza():
    from core.subscriptions import subscription_registry

    cap = _ProveedorDePrueba()
    previo = subscription_registry.get("pack_cobranza")
    subscription_registry.register("pack_cobranza", cap)
    yield cap
    if previo is None:
        subscription_registry.unregister("pack_cobranza")
    else:
        subscription_registry.register("pack_cobranza", previo)


async def _negocio(packs: list[str]) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("agn")
    tid, _ = await onboard_tenant(name=f"Negocio {slug}", slug=slug, packs=packs)
    return tid


async def _pedido_pagado_de_plan(tid: uuid.UUID) -> uuid.UUID:
    from pack_commerce.models import Order, OrderItem, Product, ProductVariant

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            product = Product(tenant_id=tid, name="Basic", description="Plan", has_variants=False)
            s.add(product)
            await s.flush()
            variant = ProductVariant(
                tenant_id=tid, product_id=product.id, sku="PLAN-BASIC", price=99000, billing_period="mensual"
            )
            s.add(variant)
            await s.flush()
            order = Order(
                tenant_id=tid, conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(),
                status="abierto", payment_status="pagado", total=99000,
                idempotency_key=str(uuid.uuid4()), order_seq=1, public_order_number="OC-0001",
            )
            s.add(order)
            await s.flush()
            s.add(OrderItem(tenant_id=tid, order_id=order.id, variant_id=variant.id, quantity=1, unit_price=99000))
            await s.flush()
            return order.id


async def _activar(tid: uuid.UUID, order_id: uuid.UUID) -> int:
    from pack_commerce.models import Order
    from pack_commerce.planes import activar_planes_del_pedido

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            order = await s.get(Order, order_id)
            return await activar_planes_del_pedido(s, order)


async def _pedido(tid: uuid.UUID, order_id: uuid.UUID):
    from pack_commerce.models import Order

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return await s.get(Order, order_id)


async def _suscripciones_de_pack_subscriptions(tid: uuid.UUID) -> list:
    from pack_subscriptions.models import Subscription

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list((await s.execute(select(Subscription))).scalars().all())


async def test_eleccion_por_configuracion_activa_en_el_proveedor_elegido(proveedor_cobranza) -> None:
    """Gherkin «Elección por configuración»: provider = pack_cobranza → se crea allí."""
    from core.subscriptions import guardar_proveedor

    tid = await _negocio(["pack_commerce", "pack_subscriptions", "pack_cobranza"])
    await guardar_proveedor(tid, "pack_cobranza")
    order_id = await _pedido_pagado_de_plan(tid)

    assert await _activar(tid, order_id) == 1
    assert len(proveedor_cobranza.activaciones) == 1
    assert proveedor_cobranza.activaciones[0]["plan"]["nombre"] == "Basic"
    assert await _suscripciones_de_pack_subscriptions(tid) == []
    assert (await _pedido(tid, order_id)).plan_activation_status == "activada"


async def test_ambiguedad_deja_pendiente_avisa_y_se_reintenta(proveedor_cobranza) -> None:
    """Gherkin «Ambigüedad»: pagado, activación pendiente visible, aviso y reintento."""
    from pack_commerce.jobs import _reintentar_activaciones

    from core.db.models import AuditLog
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.subscriptions import estado_proveedor, guardar_proveedor

    tid = await _negocio(["pack_commerce", "pack_subscriptions", "pack_cobranza"])
    assert (await estado_proveedor(tid))["estado"] == "ambiguo"
    order_id = await _pedido_pagado_de_plan(tid)

    assert await _activar(tid, order_id) == 0
    order = await _pedido(tid, order_id)
    assert order.payment_status == "pagado"
    assert order.plan_activation_status == "pendiente"
    assert "Elige uno" in (order.plan_activation_error or "")
    # Un segundo intento sin cambios no vuelve a avisar.
    await _activar(tid, order_id)
    async with tenant_scope(tid):
        async with get_session() as s:
            avisos = (
                await s.execute(
                    select(AuditLog).where(AuditLog.tenant_id == tid, AuditLog.action == "activacion_plan_pendiente")
                )
            ).scalars().all()
    assert len(avisos) == 1

    await guardar_proveedor(tid, "pack_subscriptions")
    await _reintentar_activaciones()
    order = await _pedido(tid, order_id)
    assert order.plan_activation_status == "activada"
    assert order.plan_activation_error is None
    subs = await _suscripciones_de_pack_subscriptions(tid)
    assert [s.concepto for s in subs] == ["Basic"]
    assert proveedor_cobranza.activaciones == []


async def test_sin_proveedor_el_panel_impide_crear_el_plan() -> None:
    """Gherkin «Sin proveedor»: crear un producto recurrente explica qué activar."""
    from pack_commerce.admin_api import ProductSave, save_product

    from core.api.deps import Principal
    from core.api.errors import ApiError
    from core.db.models import Admin
    from core.shared.tenant import tenant_scope

    tid = await _negocio(["pack_commerce"])
    admin = Admin(
        id=uuid.uuid4(), email="admin@solvershub.test", username=None,
        password_hash="x", role="master", active=True,
    )
    principal = Principal(admin=admin, tenant_id=tid, tenants=[], permissions={"pack_commerce": "write"})
    async with tenant_scope(tid):
        with pytest.raises(ApiError) as exc:
            await save_product(ProductSave(name="Plan mensual", price=50000, billing_period="mensual"), principal)
        # Un producto normal sí se crea.
        await save_product(ProductSave(name="Camiseta", price=50000), principal)
    assert exc.value.code == "sin_proveedor_de_suscripciones"
    assert "Suscripciones" in exc.value.message


async def test_config_rechaza_un_proveedor_no_habilitado() -> None:
    from core.api.deps import Principal
    from core.api.errors import ApiError
    from core.api.routers.settings import SubscriptionProviderUpdate, save_subscription_provider
    from core.db.models import Admin
    from core.shared.tenant import tenant_scope

    tid = await _negocio(["pack_commerce", "pack_subscriptions"])
    admin = Admin(
        id=uuid.uuid4(), email="admin@solvershub.test", username=None,
        password_hash="x", role="master", active=True,
    )
    principal = Principal(admin=admin, tenant_id=tid, tenants=[], permissions={"settings": "write"})
    async with tenant_scope(tid):
        with pytest.raises(ApiError):
            await save_subscription_provider(SubscriptionProviderUpdate(provider="pack_cobranza"), principal)
        out = await save_subscription_provider(SubscriptionProviderUpdate(provider="pack_subscriptions"), principal)
    assert (out.estado, out.configurado, out.efectivo) == ("ok", "pack_subscriptions", "pack_subscriptions")
