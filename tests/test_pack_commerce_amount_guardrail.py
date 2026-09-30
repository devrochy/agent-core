"""`_amount_guardrail`: verificación de montos, extracción anidada por clave.

Reproduce el caso real que escalaba en producción de test (tenant `yopoldo`,
2026-09-11): `consultar_inventario` devuelve el precio anidado dentro de
`matches[]`, no en el nivel superior del resultado -- el guardrail viejo
(`for value in result.values(): if isinstance(value, (int, float))`) nunca
podía verlo y bloqueaba CUALQUIER respuesta que mencionara un precio real de
catálogo, siempre, no de forma intermitente.
"""

from __future__ import annotations

import pytest
from pack_commerce.module import CRMModule, _extract_monetary_values


def test_extract_monetary_values_finds_nested_price_in_list():
    """El caso real: consultar_inventario -> {"matches": [{"price": 60000.0}]}."""
    result = {
        "matches": [
            {"sku": "001", "name": "Pollo Broaster", "price": 60000.0, "stock": 100, "disponible": True}
        ]
    }
    assert _extract_monetary_values(result) == [60000.0]


def test_extract_monetary_values_ignores_non_money_keys():
    """stock/quantity no deben colarse como "monto verificado"."""
    result = {"matches": [{"price": 45000, "stock": 12}]}
    assert _extract_monetary_values(result) == [45000.0]


def test_extract_monetary_values_mixed_top_level_and_nested():
    """generar_cotizacion: subtotal/total en el nivel superior, unit_price/line_total anidados."""
    result = {
        "items": [
            {"unit_price": 50000, "line_total": 100000, "quantity": 2},
            {"unit_price": 30000, "line_total": 30000, "quantity": 1},
        ],
        "subtotal": 130000,
        "discount": 13000,
        "total": 117000,
        "status": "draft",
    }
    assert sorted(_extract_monetary_values(result)) == sorted(
        [50000.0, 100000.0, 30000.0, 30000.0, 130000.0, 13000.0, 117000.0]
    )


@pytest.mark.asyncio
async def test_amount_guardrail_passes_with_nested_price():
    """El caso real reproducido de punta a punta: no debe escalar."""
    module = CRMModule()
    tool_results = [
        {
            "matches": [
                {"sku": "001", "name": "Pollo Broaster", "price": 60000.0, "stock": 100, "disponible": True}
            ]
        }
    ]
    verdict = await module._amount_guardrail(
        "¡Perfecto! Tu Pollo Broaster cuesta $60,000. ¿Confirmás el pedido?",
        tool_results,
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_amount_guardrail_still_blocks_hallucinated_amount():
    """Un monto que ninguna tool devolvió sigue bloqueando (no se rompió la protección real)."""
    module = CRMModule()
    tool_results = [{"matches": [{"price": 60000.0}]}]
    verdict = await module._amount_guardrail(
        "Tu pedido especial cuesta $999,999 en total.",
        tool_results,
    )
    assert not verdict.passed
    assert "999" in verdict.reason


@pytest.mark.asyncio
async def test_amount_guardrail_passes_with_generar_cotizacion_shape():
    """Forma mixta real de generar_cotizacion (total top-level + items anidados)."""
    module = CRMModule()
    tool_results = [
        {
            "items": [{"unit_price": 60000.0, "line_total": 60000.0, "quantity": 1}],
            "subtotal": 60000.0,
            "total": 60000.0,
            "status": "draft",
        }
    ]
    verdict = await module._amount_guardrail(
        "Tu cotización queda en $60,000 en total.",
        tool_results,
    )
    assert verdict.passed, verdict.reason
