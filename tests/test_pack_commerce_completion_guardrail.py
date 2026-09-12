"""`_completion_guardrail`: el modelo no puede afirmar que completó una acción
de negocio (pedido confirmado, link de pago enviado, asesor contactado) sin
haber llamado la tool correspondiente en ese mismo turno.

Reproduce el incidente real (tenant `yopoldo`, 2026-09-11): tras 2 intentos
fallidos de `generar_cotizacion` (variant_id inventado, ya corregido en un fix
anterior), el modelo terminó diciendo "¡Listo, Robinson! Cerré tu pedido. 🎉
Ahora te envío el link de pago por $54.000 [...] En un momentico el asesor te
pasa el link" -- sin haber llamado `crear_pedido`, `generar_link_pago` NI
`escalate_to_human`. El cliente quedaba esperando algo que nunca iba a pasar.
"""

from __future__ import annotations

import pytest
from pack_commerce.module import CRMModule

REAL_INCIDENT_TEXT = (
    "¡Listo, Robinson! Cerré tu pedido. 🎉\n\n"
    "Ahora te envío el link de pago por $54.000 para que lo hagas en línea.\n\n"
    "En un momentico el asesor te pasa el link y queda todo confirmado. "
    "¡Gracias por tu compra!"
)


def _tool_result(name: str, **fields) -> dict:
    return {**fields, "_tool_name": name}


@pytest.mark.asyncio
async def test_real_incident_text_fails_with_no_tool_calls():
    """El caso real exacto: cero tool_results -- ninguna de las 3 afirmaciones
    (pedido/link/asesor) tiene respaldo."""
    module = CRMModule()
    verdict = await module._completion_guardrail(REAL_INCIDENT_TEXT, [])
    assert not verdict.passed
    assert "pedido" in verdict.reason


@pytest.mark.asyncio
async def test_pedido_confirmado_passes_when_crear_pedido_called():
    module = CRMModule()
    tool_results = [_tool_result("crear_pedido", status="confirmed", public_order_number="OC-0001")]
    verdict = await module._completion_guardrail(
        "¡Listo! Tu pedido quedó confirmado, número OC-0001.", tool_results
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_pedido_confirmado_blocks_without_crear_pedido():
    module = CRMModule()
    verdict = await module._completion_guardrail("Cerré tu pedido, ya está confirmado.", [])
    assert not verdict.passed


@pytest.mark.asyncio
async def test_link_de_pago_passes_when_generar_link_pago_called():
    module = CRMModule()
    tool_results = [_tool_result("generar_link_pago", reference="OC-0001", status="pendiente")]
    verdict = await module._completion_guardrail(
        "Aquí tienes el link de pago: https://checkout.wompi.co/l/abc", tool_results
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_link_de_pago_blocks_without_generar_link_pago():
    module = CRMModule()
    verdict = await module._completion_guardrail("Te envío el link ahora mismo.", [])
    assert not verdict.passed
    assert "link_de_pago" in verdict.reason


@pytest.mark.asyncio
async def test_asesor_humano_passes_when_escalate_to_human_called():
    module = CRMModule()
    tool_results = [_tool_result("escalate_to_human", status="escalated")]
    verdict = await module._completion_guardrail(
        "Ya te comuniqué con un asesor humano.", tool_results
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_asesor_humano_blocks_without_escalate_to_human():
    module = CRMModule()
    verdict = await module._completion_guardrail(
        "En un momentico te contactan por este mismo chat.", []
    )
    assert not verdict.passed
    assert "asesor_humano" in verdict.reason


@pytest.mark.asyncio
async def test_response_without_any_claim_always_passes():
    module = CRMModule()
    verdict = await module._completion_guardrail(
        "¿Qué te gustaría pedir hoy? Tenemos pollo asado y broaster.", []
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_consultar_estado_pedido_also_satisfies_pedido_claim():
    """Confirmar el estado de un pedido YA existente (turno posterior) es
    legítimo -- no hace falta volver a llamar crear_pedido."""
    module = CRMModule()
    tool_results = [_tool_result("consultar_estado_pedido", found=True, status="pagado")]
    verdict = await module._completion_guardrail(
        "Tu pedido OC-0001 ya está confirmado y pagado.", tool_results
    )
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_dato_de_contacto_guardado_blocks_without_lead_tool():
    """El hallazgo de ayer (evals, calificar_lead/actualizar_lead): el modelo
    dice que guardó un dato de contacto sin haber llamado ninguna tool."""
    module = CRMModule()
    verdict = await module._completion_guardrail("Listo, ya guardé tu teléfono.", [])
    assert not verdict.passed
    assert "contacto" in verdict.reason


@pytest.mark.asyncio
async def test_dato_de_contacto_guardado_passes_when_actualizar_lead_called():
    module = CRMModule()
    tool_results = [_tool_result("actualizar_lead", lead_id="x", status="updated")]
    verdict = await module._completion_guardrail("Listo, ya guardé tu teléfono.", tool_results)
    assert verdict.passed, verdict.reason


@pytest.mark.asyncio
async def test_cerre_tu_pedido_does_not_require_a_lead_tool():
    """"Cerré tu pedido" es la categoría "pedido", no
    "dato_de_contacto_guardado" -- no debe exigir una tool de lead."""
    module = CRMModule()
    tool_results = [_tool_result("crear_pedido", status="confirmed", public_order_number="OC-0001")]
    verdict = await module._completion_guardrail(
        "Cerré tu pedido, quedó confirmado con el número OC-0001.", tool_results
    )
    assert verdict.passed, verdict.reason
