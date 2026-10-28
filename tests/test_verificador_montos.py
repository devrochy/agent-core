"""Verificación de montos (antes `crm_amount_check` de pack-commerce, ahora el
verificador del core, ADR-0119) y extracción anidada por clave (`core.shared.dinero`).

Reproduce el caso real que escalaba en producción de test (tenant `yopoldo`,
2026-09-11): `consultar_inventario` devuelve el precio anidado dentro de
`matches[]`, no en el nivel superior del resultado -- el guardrail viejo
(`for value in result.values(): if isinstance(value, (int, float))`) nunca
podía verlo y bloqueaba CUALQUIER respuesta que mencionara un precio real de
catálogo, siempre, no de forma intermitente.
"""

from __future__ import annotations

import pytest
from pack_commerce.module import CRMModule

from core.shared.dinero import extract_monetary_values as _extract_monetary_values
from core.shared.dinero import parse_money


async def _verificar(module, texto: str, resultados: list[dict]):
    """El texto pasa por el verificador del core con las afirmaciones del pack."""
    from core.orchestrator.verificador import Evidencia, verificar

    return verificar(texto, Evidencia.desde(resultados), await module._afirmaciones())


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
    verdict = await _verificar(module, 
        "¡Perfecto! Tu Pollo Broaster cuesta $60,000. ¿Confirmás el pedido?",
        tool_results,
    )
    assert verdict.passed, verdict.motivo


@pytest.mark.asyncio
async def test_amount_guardrail_still_blocks_hallucinated_amount():
    """Un monto que ninguna tool devolvió sigue bloqueando (no se rompió la protección real)."""
    module = CRMModule()
    tool_results = [{"matches": [{"price": 60000.0}]}]
    verdict = await _verificar(module, 
        "Tu pedido especial cuesta $999,999 en total.",
        tool_results,
    )
    assert not verdict.passed
    assert verdict.motivo == "monto_sin_evidencia"


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
    verdict = await _verificar(module, 
        "Tu cotización queda en $60,000 en total.",
        tool_results,
    )
    assert verdict.passed, verdict.motivo


# ---------------------------------------------------------------- HU-COM-02
@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("$1.800.000", 1_800_000.0),
        ("1.800.000", 1_800_000.0),
        ("$1.800.000,50", 1_800_000.5),
        ("COP 1.800.000", 1_800_000.0),
        ("$ 15", 15.0),
        ("15", 15.0),
        ("$60,000", 60_000.0),
        ("60.000", 60_000.0),
        ("", None),
        ("sin cifras", None),
    ],
)
def test_parse_money_formatos(texto: str, esperado: float | None) -> None:
    assert parse_money(texto) == esperado


@pytest.mark.asyncio
async def test_amount_guardrail_acepta_montos_grandes():
    """Un total de 1.800.000 mencionado como '$1.800.000' no debe bloquearse.

    Antes, `f'{value:g}'` daba '1.8e+06' y nunca coincidía con '1800000':
    el plan Empresarial no se podía vender (HU-COM-02).
    """
    module = CRMModule()
    tool_results = [{"total": 1_800_000.0}]
    verdict = await _verificar(module, "El plan Empresarial cuesta $1.800.000.", tool_results)
    assert verdict.passed, verdict.motivo


@pytest.mark.asyncio
async def test_amount_guardrail_bloquea_monto_inventado_por_substring():
    """'$15' con total 150.000 NO debe pasar por coincidencia de texto (HU-COM-02)."""
    module = CRMModule()
    tool_results = [{"total": 150_000.0}]
    verdict = await _verificar(module, "Te queda en $15.", tool_results)
    assert not verdict.passed, "un monto inventado no debe colarse por substring"


def test_extract_monetary_values_de_suscripciones_y_pagos():
    """HU-SUB-07: los montos de las tools de suscripción y de pagos son verificables
    (antes, mencionar la diferencia de un cambio de plan se bloqueaba y escalaba)."""
    resultado = {
        "plan": "Plan Basic",
        "valor_periodo": 200000.0,
        "cargo_cambio": {"monto": 400000.0, "link": "https://pagos/x"},
        "dias": 3,
    }
    assert sorted(_extract_monetary_values(resultado)) == [200000.0, 400000.0]
    # `amount` lo ponía el LLM en `generar_link_pago` (retirada): no cuenta como verificado.
    assert _extract_monetary_values({"amount": 150000.0, "reference": "OC-1"}) == []


def test_extract_monetary_values_de_la_cotizacion_de_un_plan_con_promocion():
    """HU-SUB-33: precio de lista, descuento y total del primer cargo son montos del servidor; sin estas claves
    el verificador bloquea una respuesta que menciona «$80.000» tras una promoción del 20 %."""
    cotizacion = {
        "plan": "Premium",
        "precio_lista": 100000.0,
        "descuento": 20000.0,
        "total_primer_cargo": 80000.0,
        "precio_periodo_siguiente": 100000.0,
        "cargo_inscripcion": {"monto": 80000.0, "monto_bruto": 100000.0},
        "trial_dias": 0,
    }
    assert sorted(_extract_monetary_values(cotizacion)) == sorted(
        [100000.0, 20000.0, 80000.0, 100000.0, 80000.0, 100000.0]
    )
