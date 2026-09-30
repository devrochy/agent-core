"""Gate determinístico de confirmación: `crear_pedido` exige que la
cotización haya pasado por `confirmar_cotizacion` (`Quote.status ==
"aceptada"`).

Incidente real (tenant `yopoldo`, 2026-09-12): el modelo armaba bien la
cotización (`generar_cotizacion` + `aplicar_promocion` reales, total
$54.000) pero afirmaba haber cerrado el pedido en texto sin llamar
`crear_pedido` -- `crm_completion_check` lo detectaba DESPUÉS de
redactado, nunca lo prevenía. Comparado con `agent-sale`
(`loop.ts::FORZAR_SIGUIENTE_TOOL`/`esTurnoSilencioso`, que lo hacen
imposible por diseño con tool_choice forzado + corte de turno): acá se
resuelve con una precondición de datos en la propia tool -- `Quote.status`
ya declaraba "aceptada" en el schema (`models.py:186`) pero ningún código
lo escribía jamás. Esta suite prueba que ahora sí: `crear_pedido` rechaza
sin el paso de confirmación, y funciona una vez que `confirmar_cotizacion`
lo marcó.

Usa la BD real (no hay FK física en este pack -- "FK lógica", ver
`models.py:4` -- así que `QuoteItem.variant_id` puede ser cualquier UUID,
no hace falta un `Product`/`ProductVariant` real para estos tests).
"""

from __future__ import annotations

import os
import uuid

import pytest
from conftest import TEST_ALPHA_ID, TEST_BETA_ID
from pack_commerce.module import MONEY_IN_TEXT

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID


async def _make_quote(session, *, status: str, total: float = 60_000.0):
    from pack_commerce.models import Quote, QuoteItem

    quote = Quote(
        tenant_id=T1,
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        subtotal=total,
        discount=0,
        total=total,
        status=status,
    )
    session.add(quote)
    await session.flush()
    session.add(
        QuoteItem(
            tenant_id=T1,
            quote_id=quote.id,
            variant_id=uuid.uuid4(),
            quantity=1,
            unit_price=total,
        )
    )
    return quote


@pytest.fixture
def fake_tool_context():
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
    )


async def test_crear_pedido_rechaza_sin_confirmar_cotizacion(fake_tool_context):
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            quote = await _make_quote(session, status="draft")
            quote_id = str(quote.id)

        skill = OrderSkill()
        result = await skill.crear_pedido(
            {
                "quote_id": quote_id,
                "payment_method": "efectivo_contraentrega",
                "delivery_method": "domicilio",
                "customer_data": {
                    "full_name": "Test Cliente",
                    "id_document": "123",
                    "address": "Calle 1",
                    "municipality": "Manizales",
                    "city": "Manizales",
                },
            },
            fake_tool_context,
        )

    assert "error" in result, result
    assert "confirmad" in result["error"].lower(), result
    assert "confirmar_cotizacion" in result.get("hint", ""), result


async def test_confirmar_cotizacion_transiciona_a_aceptada():
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            quote = await _make_quote(session, status="draft")
            quote_id = str(quote.id)

        skill = OrderSkill()
        result = await skill.confirmar_cotizacion({"quote_id": quote_id}, None)
        assert result.get("status") == "aceptada", result

        from pack_commerce.models import Quote

        async with get_session() as session:
            refreshed = await session.get(Quote, quote_id)
            assert refreshed.status == "aceptada"


async def test_crear_pedido_funciona_despues_de_confirmar_cotizacion(fake_tool_context):
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            quote = await _make_quote(session, status="draft", total=60_000.0)
            quote_id = str(quote.id)

        skill = OrderSkill()
        confirm_result = await skill.confirmar_cotizacion({"quote_id": quote_id}, fake_tool_context)
        assert confirm_result.get("status") == "aceptada", confirm_result

        result = await skill.crear_pedido(
            {
                "quote_id": quote_id,
                "payment_method": "efectivo_contraentrega",
                "delivery_method": "domicilio",
                "customer_data": {
                    "full_name": "Test Cliente",
                    "id_document": "123",
                    "address": "Calle 1",
                    "municipality": "Manizales",
                    "city": "Manizales",
                },
            },
            fake_tool_context,
        )

    assert result.get("status") == "confirmed", result
    assert "order_id" in result, result

    from pack_commerce.models import Quote

    async with tenant_scope(T1):
        async with get_session() as session:
            refreshed = await session.get(Quote, quote_id)
            assert refreshed.status == "convertida"


async def test_confirmar_cotizacion_rechaza_si_ya_se_convirtio(fake_tool_context):
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            quote = await _make_quote(session, status="convertida")
            quote_id = str(quote.id)

        skill = OrderSkill()
        result = await skill.confirmar_cotizacion({"quote_id": quote_id}, fake_tool_context)

    assert "error" in result, result


async def test_confirmar_cotizacion_no_ve_cotizacion_de_otro_tenant(fake_tool_context):
    """Hallazgo de `auditor-multitenant` (2026-09-12): el gate nuevo depende
    de RLS (no filtraba `tenant_id` explícito en Python, ya corregido como
    defensa en profundidad) -- esta es la prueba de regresión que faltaba
    para ese caso: una cotización de un tenant no debe ser visible/editable
    desde el `tenant_scope` de otro."""
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            quote = await _make_quote(session, status="draft")
            quote_id = str(quote.id)

    skill = OrderSkill()
    async with tenant_scope(TEST_BETA_ID):
        confirm_result = await skill.confirmar_cotizacion({"quote_id": quote_id}, fake_tool_context)
        assert "error" in confirm_result, confirm_result

        pedido_result = await skill.crear_pedido(
            {
                "quote_id": quote_id,
                "payment_method": "efectivo_contraentrega",
                "delivery_method": "domicilio",
                "customer_data": {
                    "full_name": "Otro Tenant",
                    "id_document": "999",
                    "address": "Calle 2",
                    "municipality": "Manizales",
                    "city": "Manizales",
                },
            },
            fake_tool_context,
        )
        assert "error" in pedido_result, pedido_result

    # Confirmamos que, desde el tenant dueño, la cotización sigue intacta
    # (nadie la tocó desde el otro tenant_scope).
    from pack_commerce.models import Quote

    async with tenant_scope(T1):
        async with get_session() as session:
            refreshed = await session.get(Quote, quote_id)
            assert refreshed.status == "draft"


async def test_crear_pedido_rechaza_pago_en_linea_bajo_el_minimo(fake_tool_context):
    """Incidente real (tenant `yopoldo`, 2026-09-12): un pedido de $54.000
    con `payment_method="pago_en_linea"` nunca podía completarse -- el
    mínimo real de la pasarela es $150.000 (`MIN_AMOUNT_COP` en
    `pack_payments/providers/wompi.py`, no importable acá -- invariante 5).
    Antes esto solo se descubría (si el modelo llegaba a llamarla) en
    `generar_link_pago`, ya tarde para redirigir la conversación con
    naturalidad -- el modelo terminó afirmando que mandó el link sin haber
    llamado ninguna tool, y escaló. Ahora `crear_pedido` lo rechaza de
    entrada, determinístico, sin depender de que la cuenta la haga bien
    el LLM.

    El mensaje de error NO incluye el monto exacto (segundo incidente real,
    mismo día): la primera versión sí lo incluía en texto libre, el modelo
    lo repetía en su respuesta y `crm_amount_check` lo bloqueaba por "monto
    no verificado" (solo reconoce montos que salen de una clave de
    MONEY_KEYS en un resultado de tool, no números embebidos en un string
    de error) -- verificado en vivo contra el proceso desplegado real."""
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            quote = await _make_quote(session, status="aceptada", total=54_000.0)
            quote_id = str(quote.id)

        skill = OrderSkill()
        result = await skill.crear_pedido(
            {
                "quote_id": quote_id,
                "payment_method": "pago_en_linea",
                "delivery_method": "domicilio",
                "customer_data": {
                    "full_name": "Test Cliente",
                    "id_document": "123",
                    "address": "Calle 1",
                    "municipality": "Manizales",
                    "city": "Manizales",
                },
            },
            fake_tool_context,
        )

    assert "error" in result, result
    assert not MONEY_IN_TEXT.search(result["error"]), (
        f"el error no debe incluir un monto en texto libre (dispara crm_amount_check "
        f"como 'no verificado'): {result['error']!r}"
    )
    assert "transferencia" in result.get("hint", "").lower() or "contraentrega" in result.get(
        "hint", ""
    ).lower(), result

    # No debe haber creado ningún pedido.
    from pack_commerce.models import Order
    from sqlalchemy import select

    async with tenant_scope(T1):
        async with get_session() as session:
            existing = (
                await session.execute(select(Order).where(Order.quote_id == quote_id))
            ).scalar_one_or_none()
            assert existing is None, existing


async def test_crear_pedido_acepta_pago_en_linea_sobre_el_minimo(fake_tool_context):
    """Control: el gate nuevo no bloquea pedidos legítimos por encima del
    mínimo -- un total de $200.000 con pago_en_linea debe crear el pedido
    normalmente."""
    from pack_commerce.skills import OrderSkill

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            quote = await _make_quote(session, status="aceptada", total=200_000.0)
            quote_id = str(quote.id)

        skill = OrderSkill()
        result = await skill.crear_pedido(
            {
                "quote_id": quote_id,
                "payment_method": "pago_en_linea",
                "delivery_method": "domicilio",
                "customer_data": {
                    "full_name": "Test Cliente",
                    "id_document": "123",
                    "address": "Calle 1",
                    "municipality": "Manizales",
                    "city": "Manizales",
                },
            },
            fake_tool_context,
        )

    assert result.get("status") == "confirmed", result

    # Limpieza: no dejar el pedido real creado en la BD compartida de tests
    # (evita colisiones de order_seq en corridas futuras de la suite).
    from pack_commerce.models import Order, OrderItem

    async with tenant_scope(T1):
        async with get_session() as session:
            order_id = result["order_id"]
            await session.execute(OrderItem.__table__.delete().where(OrderItem.order_id == order_id))
            await session.execute(Order.__table__.delete().where(Order.id == order_id))
