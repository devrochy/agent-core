"""Afirmaciones de cierre de pack-commerce en el verificador del core (ADR-0119,
HU-ARCH-29; antes `crm_completion_check`): el modelo no puede afirmar que
completó una acción de negocio (pedido confirmado, link de pago enviado, asesor
contactado) sin haber llamado la tool correspondiente en ese mismo turno.

Reproduce el incidente real (tenant `yopoldo`, 2026-09-11): tras 2 intentos
fallidos de `generar_cotizacion` (variant_id inventado, ya corregido en un fix
anterior), el modelo terminó diciendo "¡Listo, Robinson! Cerré tu pedido. 🎉
Ahora te envío el link de pago por $54.000 [...] En un momentico el asesor te
pasa el link" -- sin haber llamado `crear_pedido`, `confirmar_pago_pedido` NI
`escalate_to_human`. El cliente quedaba esperando algo que nunca iba a pasar.
"""

from __future__ import annotations

import pytest
from pack_commerce.module import CRMModule


async def _verificar(module, texto: str, resultados: list[dict]):
    """El texto pasa por el verificador del core con las afirmaciones del pack."""
    from core.orchestrator.verificador import Evidencia, verificar

    return verificar(texto, Evidencia.desde(resultados), await module._afirmaciones())

REAL_INCIDENT_TEXT = (
    "¡Listo, Robinson! Cerré tu pedido. 🎉\n\n"
    "Ahora te envío el link de pago por $54.000 para que lo hagas en línea.\n\n"
    "En un momentico el asesor te pasa el link y queda todo confirmado. "
    "¡Gracias por tu compra!"
)

# Segundo incidente real, mismo día: el modelo esquivó las frases exactas del
# primer fix cambiando de conjugación ("Cerramos" en vez de "Cerré", "genero
# el link" en vez de "envío/paso el link") -- confirmó que matchear frases
# exactas no alcanza, hay que matchear por raíz de verbo + tema.
SECOND_INCIDENT_TEXT = (
    "¡Listo, Robinson! Cerramos tu pedido del Pollo Broaster por $60.000. 🎉\n\n"
    "Ahora mismo te genero el link de pago para que lo hagas en línea. Dame un momentico. 🍗"
)


def _tool_result(name: str, **fields) -> dict:
    return {**fields, "_tool_name": name}


@pytest.mark.asyncio
async def test_real_incident_text_fails_with_no_tool_calls():
    """El caso real exacto: cero tool_results -- ninguna de las 3 afirmaciones
    (pedido/link/asesor) tiene respaldo."""
    module = CRMModule()
    verdict = await _verificar(module, REAL_INCIDENT_TEXT, [])
    assert not verdict.passed
    assert "pedido" in verdict.afirmacion


@pytest.mark.asyncio
async def test_second_incident_text_fails_despite_different_conjugation():
    """El segundo incidente real, mismo día: "Cerramos" (no "Cerré"), "genero
    el link" (no "envío"/"paso") -- esquivó el primer fix cambiando de verbo.
    Confirma que la versión ampliada de los patrones (por raíz, no frase
    exacta) sigue bloqueando."""
    module = CRMModule()
    verdict = await _verificar(module, SECOND_INCIDENT_TEXT, [])
    assert not verdict.passed


@pytest.mark.asyncio
async def test_pedido_confirmado_passes_when_crear_pedido_called():
    module = CRMModule()
    tool_results = [_tool_result("crear_pedido", status="confirmed", public_order_number="OC-0001")]
    verdict = await _verificar(module, 
        "¡Listo! Tu pedido quedó confirmado, número OC-0001.", tool_results
    )
    assert verdict.passed, verdict.motivo


@pytest.mark.asyncio
async def test_pedido_confirmado_blocks_without_crear_pedido():
    module = CRMModule()
    verdict = await _verificar(module, "Cerré tu pedido, ya está confirmado.", [])
    assert not verdict.passed


@pytest.mark.asyncio
async def test_link_de_pago_passes_when_confirmar_pago_pedido_called():
    module = CRMModule()
    tool_results = [_tool_result("confirmar_pago_pedido", order_id="o-1", status="link_creado")]
    verdict = await _verificar(module, 
        "Aquí tienes el link de pago: https://checkout.wompi.co/l/abc", tool_results
    )
    assert verdict.passed, verdict.motivo


@pytest.mark.asyncio
async def test_link_de_pago_blocks_without_confirmar_pago_pedido():
    module = CRMModule()
    verdict = await _verificar(module, "Te envío el link de pago ahora mismo.", [])
    assert not verdict.passed
    assert "link_de_pago" in verdict.afirmacion


@pytest.mark.asyncio
async def test_generic_link_mention_without_de_pago_does_not_trigger():
    """"El link" a secas (sin "de pago"/"para pagar") es deliberadamente
    ambiguo -- podría ser cualquier link (ej. uno de Calendar) y exigirle acá
    una tool de link de pago daría falsos positivos fuera del dominio de pagos."""
    module = CRMModule()
    verdict = await _verificar(module, "Te envío el link ahora mismo.", [])
    assert verdict.passed, verdict.motivo


@pytest.mark.asyncio
async def test_asesor_humano_passes_when_escalate_to_human_called():
    module = CRMModule()
    tool_results = [_tool_result("escalate_to_human", status="escalated")]
    verdict = await _verificar(module, 
        "Ya te comuniqué con un asesor humano.", tool_results
    )
    assert verdict.passed, verdict.motivo


@pytest.mark.asyncio
async def test_asesor_humano_blocks_without_escalate_to_human():
    module = CRMModule()
    verdict = await _verificar(module, 
        "En un momentico te contactan por este mismo chat.", []
    )
    assert not verdict.passed
    assert "asesor_humano" in verdict.afirmacion


@pytest.mark.asyncio
async def test_response_without_any_claim_always_passes():
    module = CRMModule()
    verdict = await _verificar(module, 
        "¿Qué te gustaría pedir hoy? Tenemos pollo asado y broaster.", []
    )
    assert verdict.passed, verdict.motivo


@pytest.mark.asyncio
async def test_consultar_estado_pedido_also_satisfies_pedido_claim():
    """Confirmar el estado de un pedido YA existente (turno posterior) es
    legítimo -- no hace falta volver a llamar crear_pedido."""
    module = CRMModule()
    tool_results = [_tool_result("consultar_estado_pedido", found=True, status="pagado")]
    verdict = await _verificar(module, 
        "Tu pedido OC-0001 ya está confirmado y pagado.", tool_results
    )
    assert verdict.passed, verdict.motivo


@pytest.mark.asyncio
async def test_dato_de_contacto_guardado_blocks_without_lead_tool():
    """El hallazgo de ayer (evals, calificar_lead/actualizar_lead): el modelo
    dice que guardó un dato de contacto sin haber llamado ninguna tool."""
    module = CRMModule()
    verdict = await _verificar(module, "Listo, ya guardé tu teléfono.", [])
    assert not verdict.passed
    assert "contacto" in verdict.afirmacion


@pytest.mark.asyncio
async def test_dato_de_contacto_guardado_passes_when_actualizar_lead_called():
    module = CRMModule()
    tool_results = [_tool_result("actualizar_lead", lead_id="x", status="updated")]
    verdict = await _verificar(module, "Listo, ya guardé tu teléfono.", tool_results)
    assert verdict.passed, verdict.motivo


@pytest.mark.asyncio
async def test_cerre_tu_pedido_does_not_require_a_lead_tool():
    """"Cerré tu pedido" es la categoría "pedido", no
    "dato_de_contacto_guardado" -- no debe exigir una tool de lead."""
    module = CRMModule()
    tool_results = [_tool_result("crear_pedido", status="confirmed", public_order_number="OC-0001")]
    verdict = await _verificar(module, 
        "Cerré tu pedido, quedó confirmado con el número OC-0001.", tool_results
    )
    assert verdict.passed, verdict.motivo


# -------------------------------------------------------------------------
# ADR-0025: claims desactivados/custom por tenant (CRMConfig)
# -------------------------------------------------------------------------


def _mock_config(monkeypatch: pytest.MonkeyPatch, config) -> None:
    """Monkeypatchea `plugin_manager.load_config` para que
    `_afirmaciones` reciba un `CRMConfig` fijo, sin BD ni
    tenant_scope (mismo criterio que el resto de tests unitarios de este
    módulo -- son puros)."""
    import core.plugin_manager as plugin_manager_mod

    async def _fake_load_config(plugin_id):
        return config

    monkeypatch.setattr(plugin_manager_mod.plugin_manager, "load_config", _fake_load_config)


@pytest.mark.asyncio
async def test_claim_incorporado_desactivado_ya_no_bloquea(monkeypatch: pytest.MonkeyPatch):
    from pack_commerce.config import CRMConfig

    _mock_config(monkeypatch, CRMConfig(completion_claims_disabled="link_de_pago"))
    module = CRMModule()
    verdict = await _verificar(module, "Te envío el link de pago ahora mismo.", [])
    assert verdict.passed, verdict.motivo


@pytest.mark.asyncio
async def test_claim_no_desactivado_sigue_bloqueando(monkeypatch: pytest.MonkeyPatch):
    """Desactivar UN claim no apaga los demás."""
    from pack_commerce.config import CRMConfig

    _mock_config(monkeypatch, CRMConfig(completion_claims_disabled="link_de_pago"))
    module = CRMModule()
    verdict = await _verificar(module, "Cerré tu pedido, ya está confirmado.", [])
    assert not verdict.passed
    assert "pedido" in verdict.afirmacion


@pytest.mark.asyncio
async def test_claim_custom_del_tenant_bloquea_como_uno_incorporado(monkeypatch: pytest.MonkeyPatch):
    from pack_commerce.config import CRMConfig

    _mock_config(
        monkeypatch,
        CRMConfig(completion_claims_custom="agenda_cita: agendé tu cita => crear_cita"),
    )
    module = CRMModule()

    verdict_sin_tool = await _verificar(module, "Listo, ya agendé tu cita.", [])
    assert not verdict_sin_tool.passed
    assert "agenda_cita" in verdict_sin_tool.afirmacion

    verdict_con_tool = await _verificar(module, 
        "Listo, ya agendé tu cita.", [_tool_result("crear_cita", status="ok")]
    )
    assert verdict_con_tool.passed, verdict_con_tool.motivo


@pytest.mark.asyncio
async def test_claim_custom_con_mismo_nombre_reemplaza_al_incorporado(monkeypatch: pytest.MonkeyPatch):
    """Si el tenant nombra su claim custom igual que uno incorporado
    ("pedido"), el custom gana -- reemplaza las frases/tools originales."""
    from pack_commerce.config import CRMConfig

    _mock_config(
        monkeypatch,
        CRMConfig(completion_claims_custom="pedido: pedido despachado => marcar_despachado"),
    )
    module = CRMModule()

    # el texto original del claim "pedido" ("Cerré tu pedido") ya NO
    # coincide con el reemplazo -- pasa.
    verdict = await _verificar(module, "Cerré tu pedido, ya está confirmado.", [])
    assert verdict.passed, verdict.motivo

    # el nuevo texto/tool custom sí se evalúa bajo el reemplazo.
    verdict2 = await _verificar(module, "Tu pedido despachado ya va en camino.", [])
    assert not verdict2.passed
    assert "pedido" in verdict2.afirmacion


@pytest.mark.asyncio
async def test_config_ausente_o_invalida_no_rompe_el_guardrail(monkeypatch: pytest.MonkeyPatch):
    """Fail-open: si `load_config` lanza (config ausente, plugin sin
    activar, lo que sea), el guardrail sigue funcionando con los claims
    incorporados de siempre -- nunca debe romper el turno del cliente."""
    import core.plugin_manager as plugin_manager_mod

    async def _raise(plugin_id):
        raise RuntimeError("plugin no configurado")

    monkeypatch.setattr(plugin_manager_mod.plugin_manager, "load_config", _raise)
    module = CRMModule()
    verdict = await _verificar(module, REAL_INCIDENT_TEXT, [])
    assert not verdict.passed
    assert "pedido" in verdict.afirmacion


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["sub_link_pago", "sub_cambiar_plan"])
async def test_link_de_suscripcion_o_de_cambio_de_plan_tambien_respalda_el_aviso(tool):
    """El link del período de una suscripción o de un cambio de plan (tools del core) es
    evidencia igual que el de un pedido (antes terminaba en escalamiento; HU-SUB-12)."""
    module = CRMModule()
    tool_results = [_tool_result(tool, url="https://pagos.simulados/sub-1", monto=90000.0)]
    verdict = await _verificar(module, 
        "Tu cargo es de $90.000. Aquí tienes el link de pago: https://pagos.simulados/sub-1", tool_results
    )
    assert verdict.passed, verdict.motivo


# Incidente de test (tenant solvershub, 2026-10-11): el cliente aceptó los términos y el modelo,
# sin llamar `crear_pedido`, respondió esto. El tope de 3 palabras entre "pedido" y "creado"
# lo dejó pasar y el cliente quedó creyendo que su pedido existía.
INCIDENTE_PEDIDO_CREADO = (
    "¡Listo, Ana! Tu pedido del plan Premium quedó creado.\n\n"
    "Te llega el acceso a tu negocio por correo a ana@example.com.\n\n"
    "¿Querés pagar en línea ahora?"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "texto",
    [
        INCIDENTE_PEDIDO_CREADO,
        "Ya quedó creado tu pedido del plan Basic.",
        "Listo, ya quedó creado su pedido.",
        # 5 palabras antes del auxiliar: el tope del tramo largo.
        "Perfecto, el pedido de tu plan Empresarial ya quedó confirmado.",
        "Ya creé tu pedido del plan Premium.",
    ],
)
async def test_pedido_creado_sin_crear_pedido_falla(texto: str):
    verdict = await _verificar(CRMModule(), texto, [])
    assert not verdict.passed
    assert verdict.afirmacion == "pedido"


@pytest.mark.asyncio
async def test_pedido_creado_con_crear_pedido_del_turno_pasa():
    tool_results = [_tool_result("crear_pedido", status="pending", public_order_number="OC-0001")]
    verdict = await _verificar(CRMModule(), INCIDENTE_PEDIDO_CREADO, tool_results)
    assert verdict.passed, verdict.motivo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "texto",
    [
        "Tu pedido del plan Premium todavía no quedó creado: falta que aceptes los términos.",
        "Para que tu pedido del plan Premium quede creado necesito que aceptes los términos.",
        "Cuando aceptes los términos, tu pedido del plan Premium queda creado y te paso el link.",
        "¿Confirmás que el pedido del plan Premium quede creado a nombre de Tienda Ejemplo?",
        # Resúmenes antes de crear el pedido (revisión de seguridad): el participio es de otra
        # cosa o el pedido todavía no existe.
        "Resumen de tu pedido: plan Premium, 3 usuarios, datos confirmados.",
        "Tu pedido del plan Premium va a quedar creado hoy.",
    ],
)
async def test_pedido_creado_en_negacion_condicional_o_pregunta_no_afirma(texto: str):
    verdict = await _verificar(CRMModule(), texto, [])
    assert verdict.passed, verdict


@pytest.mark.asyncio
async def test_participio_de_la_cotizacion_no_afirma_el_pedido():
    """"Quedó generada" es de la cotización: con `generar_cotizacion` en el turno pasa, y el
    tramo largo de `pedido` (solo masculino) no la toma por un pedido creado."""
    tool_results = [_tool_result("generar_cotizacion", quote_id="q-1", total=600000.0)]
    verdict = await _verificar(
        CRMModule(), "Para tu pedido del plan Premium ya quedó generada la cotización.", tool_results
    )
    assert verdict.passed, verdict


@pytest.mark.asyncio
async def test_pedido_patron_lineal_ante_texto_armado():
    """El patrón `pedido` con el tramo de hasta 6 palabras no retrocede de forma cuadrática:
    el texto lo escribe el LLM y puede repetir contenido del cliente."""
    import time

    module = CRMModule()
    for texto in ("pedido" + "-" * 50_000 + "a", "pedido " * 20_000, "quedó " * 20_000):
        inicio = time.perf_counter()
        await _verificar(module, texto, [])
        assert time.perf_counter() - inicio < 1.0
