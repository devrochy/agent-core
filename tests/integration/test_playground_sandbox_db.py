"""Sandbox de la prueba del panel con Postgres/Redis reales (HU-ARCH-14).

- El pedido simulado no crea `Order`, no toca el consecutivo real y devuelve `PRUEBA-…`.
- Cerrar la prueba purga la conversación `test`, su cliente sintético y los mensajes.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def test_pedido_simulado_no_toca_order_ni_consecutivo(tenant_setup) -> None:
    t1, _t2 = tenant_setup
    from pack_commerce.skills import OrderSkill
    from sqlalchemy import text

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.mcp.host import ejecutar_tool
    from core.mcp.sandbox import SandboxPrueba
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t1):
        # Conversación y cliente sintéticos de la prueba.
        async with get_session() as session:
            customer = Customer(tenant_id=t1, channel="test", external_id=f"panel-{uuid.uuid4()}", name="Prueba")
            session.add(customer)
            await session.flush()
            conversation = Conversation(
                tenant_id=t1, customer_id=customer.id, channel="test", status="open", state={"is_test": True, "panel": True}
            )
            session.add(conversation)
            await session.flush()
            conversation_id, customer_id = conversation.id, customer.id

        sandbox = SandboxPrueba(t1, conversation_id)
        quote_id = str(uuid.uuid4())
        await sandbox.guardar(
            "quotes",
            quote_id,
            {
                "quote_id": quote_id,
                "customer_id": str(customer_id),
                "conversation_id": str(conversation_id),
                "status": "aceptada",
                "items": [{"product_name": "Producto", "quantity": 1, "unit_price": 1000, "line_total": 1000}],
                "subtotal": 1000.0,
                "discount": 0.0,
                "total": 1000.0,
            },
        )
        contexto = ToolContext(
            conversation_id=conversation_id,
            customer_id=customer_id,
            idempotency_key="k",
            channel="test",
            sandbox=sandbox,
        )
        tool = next(t for t in OrderSkill().get_tools() if t.name == "crear_pedido")
        async with get_session() as session:
            antes = (
                await session.execute(
                    text("select coalesce((select last_seq from pack_commerce_order_counters where tenant_id = :t), 0)"),
                    {"t": t1},
                )
            ).scalar()
            resultado = await ejecutar_tool(
                tool,
                {
                    "quote_id": quote_id,
                    "payment_method": "efectivo_contraentrega",
                    "delivery_method": "recoger_en_tienda",
                    "customer_data": {"full_name": "Cliente Prueba", "id_document": "1"},
                },
                contexto,
            )
            assert resultado["simulado"] is True
            assert resultado["public_order_number"].startswith("PRUEBA-")
            despues = (
                await session.execute(
                    text("select coalesce((select last_seq from pack_commerce_order_counters where tenant_id = :t), 0)"),
                    {"t": t1},
                )
            ).scalar()
            pedidos = (
                await session.execute(
                    text("select count(*) from pack_commerce_orders where tenant_id = :t"),
                    {"t": t1},
                )
            ).scalar()
        assert (antes, despues) == (antes, antes)
        assert pedidos == 0


async def test_cerrar_prueba_purga_conversacion_y_cliente(tenant_setup) -> None:
    t1, _t2 = tenant_setup
    from sqlalchemy import select

    from core.admin.playground import close_test_conversation, get_or_create_test_conversation
    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(t1):
        conversation = await get_or_create_test_conversation(t1)
        conversation_id = conversation.id
        async with get_session() as session:
            session.add(
                Message(
                    tenant_id=t1,
                    conversation_id=conversation_id,
                    direction="inbound",
                    sender_type="customer",
                    content="hola",
                    status="sent",
                )
            )
        cerradas = await close_test_conversation(t1)
        assert cerradas >= 1
        async with get_session() as session:
            assert await session.get(Conversation, conversation_id) is None
            mensajes = (
                await session.execute(
                    select(Message).where(Message.conversation_id == conversation_id)
                )
            ).scalars().all()
            assert mensajes == []
            # El cliente sintético se va cuando no quedan conversaciones de prueba suyas.
            clientes = (
                await session.execute(
                    select(Customer).where(Customer.tenant_id == t1, Customer.channel == "test")
                )
            ).scalars().all()
            assert clientes == []
