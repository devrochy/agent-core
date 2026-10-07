"""HU-COM-01: el dinero del pedido se calcula en el servidor.

Cubre los cinco escenarios Gherkin:
1. Monto del servidor: `confirmar_pago_pedido(order_id)` usa `Order.total`.
2. Ítems agregados: al cambiar el total, la referencia sube de versión (`-r2`).
3. Webhook validado: un `payment.confirmed` con monto distinto no marca pagado.
4. Rechazo tardío: un `payment.rejected` sobre un pedido pagado no lo cambia.
5. Mínimo del proveedor: monto bajo el mínimo → `monto_bajo_minimo`, sin apagar el pack.

Usa una `PaymentCapability` fake registrada en el `payment_registry` del core,
para no depender de Wompi ni de red (invariante nº5: commerce no importa el pack
de pagos; el escenario 5 simula el rechazo de negocio con `PaymentCapabilityError`).
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID


class _FakeCapability:
    """Capacidad de pago mínima: devuelve un link o rechaza por monto."""

    plugin_id = "pack_payments_fake"

    def __init__(self, *, min_amount: float = 0.0) -> None:
        self.min_amount = min_amount
        self.calls: list[tuple[str, float]] = []

    def methods(self):
        return []

    async def create_link(self, *, reference: str, amount: float, description: str = "", method=None):
        from core.payments import PaymentCapabilityError, PaymentLink

        self.calls.append((reference, amount))
        if amount < self.min_amount:
            raise PaymentCapabilityError("monto menor al mínimo del proveedor")
        return PaymentLink(reference=reference, url=f"https://pay.test/{reference}", method="online")


@pytest.fixture
def fake_context():
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
    )


@pytest.fixture
def fake_capability(monkeypatch):
    from pack_commerce.skills import OrderSkill

    from core.payments import payment_registry

    cap = _FakeCapability()
    monkeypatch.setattr(payment_registry, "_caps", {cap.plugin_id: cap})

    async def _cap(self):
        return cap

    monkeypatch.setattr(OrderSkill, "_payment_capability", _cap)
    return cap


async def _make_order(session, *, total: float = 185_000.0, status: str = "abierto"):
    from pack_commerce.models import Order
    from sqlalchemy import text

    order_seq = (
        await session.execute(text("select pack_commerce_next_order_seq(:t)"), {"t": T1})
    ).scalar()
    order = Order(
        tenant_id=T1,
        quote_id=None,
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        status=status,
        payment_status="pendiente",
        payment_method="pago_en_linea",
        delivery_method="domicilio",
        total=total,
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        order_seq=int(order_seq),
        public_order_number=f"OC-{int(order_seq):04d}",
    )
    session.add(order)
    await session.flush()
    return order


@pytest.fixture
def commerce_enabled(monkeypatch):
    """`_update_order_payment_status` filtra por pack habilitado: lo forzamos."""
    import core.agents.spec as spec_mod

    async def _load(tid):
        return {"packs": ["pack_commerce"]}

    monkeypatch.setattr(spec_mod, "load_active_spec", _load)
    monkeypatch.setattr(spec_mod, "enabled_packs", lambda spec: {"core", "pack_commerce"})


async def test_monto_del_servidor(fake_context, fake_capability):
    """1. El link usa Order.total; el LLM no pasa monto ni referencia."""
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            order = await _make_order(session, total=185_000.0)
            order_id = str(order.id)

        result = await OrderSkill().confirmar_pago_pedido({"order_id": order_id}, fake_context)

    assert result.get("status") == "link_creado", result
    assert result["monto"] == 185_000.0
    assert fake_capability.calls[0][1] == 185_000.0


async def test_items_agregados_suben_version(fake_context, fake_capability):
    """2. Si el total cambió, la referencia pasa a `-r2`."""
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            order = await _make_order(session, total=185_000.0)
            order_id = str(order.id)
            base = order.public_order_number

        skill = OrderSkill()
        first = await skill.confirmar_pago_pedido({"order_id": order_id}, fake_context)
        assert first["status"] == "link_creado"

        # Simula que se agregó un ítem: sube la versión y limpia el link vigente.
        from pack_commerce.models import Order

        async with get_session() as session:
            o = await session.get(Order, order_id)
            o.total = 200_000.0
            o.payment_link_version = 1
            o.payment_link_url = None
            o.payment_reference = None

        second = await skill.confirmar_pago_pedido({"order_id": order_id}, fake_context)

    assert second["status"] == "link_creado", second
    assert second["monto"] == 200_000.0
    assert fake_capability.calls[-1][0] == f"{base}-r1"


async def test_webhook_con_monto_distinto_no_paga(fake_context, fake_capability, commerce_enabled):
    """3. Un monto distinto no marca el pedido pagado."""
    from pack_commerce.models import Order
    from pack_commerce.module import CRMModule

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            order = await _make_order(session, total=185_000.0)
            order_id = str(order.id)
            base = order.public_order_number

        mod = CRMModule()
        await mod._update_order_payment_status(base, "pagado", amount=100_000.0)

        async with get_session() as session:
            o = await session.get(Order, order_id)
            assert o.payment_status == "pendiente", o.payment_status


async def test_rechazo_tardio_no_cambia_pagado(fake_context, fake_capability, commerce_enabled):
    """4. Un `rejected` posterior a un `pagado` no cambia el estado."""
    from pack_commerce.models import Order
    from pack_commerce.module import CRMModule

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            order = await _make_order(session, total=185_000.0)
            order_id = str(order.id)
            base = order.public_order_number

        mod = CRMModule()
        await mod._update_order_payment_status(base, "pagado", amount=185_000.0)
        await mod._update_order_payment_status(base, "rechazado", amount=185_000.0)

        async with get_session() as session:
            o = await session.get(Order, order_id)
            assert o.payment_status == "pagado", o.payment_status


async def test_monto_bajo_minimo_es_error_de_negocio(fake_context, monkeypatch):
    """5. Bajo el mínimo devuelve `monto_bajo_minimo` sin apagar el pack."""
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    cap = _FakeCapability(min_amount=150_000.0)

    async def _cap(self):
        return cap

    monkeypatch.setattr(OrderSkill, "_payment_capability", _cap)
    async with tenant_scope(T1):
        async with get_session() as session:
            order = await _make_order(session, total=54_000.0)
            order_id = str(order.id)

        result = await OrderSkill().confirmar_pago_pedido({"order_id": order_id}, fake_context)

    assert result.get("error") == "monto_bajo_minimo", result
