"""Argumentos inválidos del LLM no deben tirar una excepción no manejada.

Reproduce el incidente real (tenant `yopoldo`, 2026-09-11): el LLM llamó
`generar_cotizacion` con `variant_id="pollo_broaster"` en vez del UUID real
que había devuelto `consultar_inventario`. El string inválido llegaba
directo a Postgres (`asyncpg.exceptions.DataError`), una excepción no
manejada que el core interpreta como una falla real del plugin y lo
desactiva -- GLOBALMENTE, para TODOS los tenants (`PluginRecord.status` no
es por-tenant). Confirmado en logs de producción de test: `pack_commerce`
y `pack_payments` quedaron en estado `error` para todo el proceso.

`auditor-multitenant` encontró 3 variantes más del mismo patrón (argumento
mal tipado -> excepción no manejada -> mismo apagón global) en el mismo
diff: `calificar_lead` (score no numérico), `generar_cotizacion`/
`agregar_item_pedido` (un elemento de `items` que no es un dict) y
`crear_pedido` (`customer_data` que no es un dict). Se corrigieron todas
en la misma tanda.
"""

from __future__ import annotations

import uuid

import pytest
from pack_commerce.skills import LeadSkill, OrderSkill, _get_variant_with_stock, _parse_uuid

from core.mcp.types import ToolContext


@pytest.fixture
def fake_tool_context() -> ToolContext:
    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
    )


def test_parse_uuid_valid():
    parsed = _parse_uuid(str(uuid.uuid4()))
    assert isinstance(parsed, uuid.UUID)


@pytest.mark.parametrize("value", ["pollo_broaster", "", "123", None, 42])
def test_parse_uuid_invalid_returns_none(value):
    assert _parse_uuid(value) is None


@pytest.mark.asyncio
async def test_get_variant_with_stock_invalid_id_returns_none_not_exception():
    """El caso real exacto: antes esto llegaba a Postgres y tiraba DataError."""
    result = await _get_variant_with_stock("pollo_broaster")
    assert result is None


@pytest.mark.asyncio
async def test_calificar_lead_non_numeric_score_returns_error(fake_tool_context):
    skill = LeadSkill()
    result = await skill.calificar_lead({"score": "alto"}, fake_tool_context)
    assert "error" in result


@pytest.mark.asyncio
async def test_generar_cotizacion_non_dict_item_returns_error(fake_tool_context):
    skill = OrderSkill()
    result = await skill.generar_cotizacion({"items": ["pollo_broaster"]}, fake_tool_context)
    assert "error" in result


@pytest.mark.asyncio
async def test_agregar_item_pedido_non_dict_item_returns_error(fake_tool_context):
    skill = OrderSkill()
    result = await skill.agregar_item_pedido(
        {"order_id": str(uuid.uuid4()), "items": [123]}, fake_tool_context
    )
    assert "error" in result


@pytest.mark.asyncio
async def test_crear_pedido_non_dict_customer_data_returns_error(fake_tool_context):
    skill = OrderSkill()
    result = await skill.crear_pedido(
        {
            "quote_id": str(uuid.uuid4()),
            "payment_method": "transferencia",
            "delivery_method": "domicilio",
            "customer_data": "no soy un dict",
        },
        fake_tool_context,
    )
    assert "error" in result
