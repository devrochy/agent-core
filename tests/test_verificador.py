"""Verificador de afirmaciones con modelo de evidencia (ADR-0119, HU-ARCH-29).

Los criterios de la HU, los falsos positivos medidos el 2026-10-09 en los
journeys (todos de `link_de_pago` en el viejo `crm_completion_check`) y los
incidentes reales que el verificador tiene que seguir atrapando.
"""

from __future__ import annotations

import re

import pytest

from core.mcp.types import Afirmacion
from core.orchestrator.verificador import (
    TOGGLES,
    Evidencia,
    instruccion_correctiva,
    normalizar_desactivados,
    verificar,
    verificar_seguro,
)

PEDIDO = Afirmacion(
    label="pedido",
    patron=re.compile(
        r"cerr\w*\s+(?:tu|el|los?|la)?\s*pedido|"
        r"pedido\b(?:\W+\w+){0,3}\W+(?:confirmad\w*|cerrad\w*|cread\w*|generad\w*)",
        re.IGNORECASE,
    ),
    tools=frozenset({"crear_pedido", "consultar_estado_pedido"}),
)
LINK = Afirmacion(
    label="link_de_pago",
    patron=re.compile(r"link\s+(?:de\s+pago|para\s+pagar)", re.IGNORECASE),
    tools=frozenset({"confirmar_pago_pedido"}),
    basta_url=True,
)
AFIRMACIONES = [PEDIDO, LINK]


def _tool(nombre: str, *, actual: bool = True, ok: bool = True, **campos) -> dict:
    return {**campos, "_tool_name": nombre, "_current_turn": actual, "_ok": ok}


def _verificar(texto: str, resultados: list[dict], contexto: str = "", desactivados=()):
    return verificar(texto, Evidencia.desde(resultados, contexto), AFIRMACIONES, desactivados)


# ---------------------------------------------------------------- montos
COTIZACION = _tool("generar_cotizacion", total=600000, subtotal=600000, status="draft")


def test_monto_con_evidencia_pasa() -> None:
    assert _verificar("El total es $600.000.", [COTIZACION]).passed


def test_monto_distinto_pide_reintento() -> None:
    veredicto = _verificar("El total es $500.000.", [COTIZACION])
    assert not veredicto.passed
    assert veredicto.motivo == "monto_sin_evidencia"
    assert veredicto.codigo == "verificador.monto_sin_evidencia"
    assert ":" not in veredicto.codigo, "el loop corta el motivo de la escalación en el primer ':'"
    assert veredicto.montos_turno == [600000]


def test_monto_de_un_turno_anterior_pasa() -> None:
    anterior = {"total": 600000, "_current_turn": False}
    assert _verificar("Sí, te confirmo: son $600.000.", [anterior]).passed


def test_cifra_abreviada_no_se_verifica() -> None:
    assert _verificar("Los planes van desde $1,8 millones o $200 mil al mes.", []).passed


@pytest.mark.asyncio
async def test_instruccion_de_monto_lista_los_montos_del_turno(monkeypatch) -> None:
    async def respuesta(_spec, clave):
        assert clave == "verificador_monto_sin_evidencia"
        return "Usa solo estos: {montos}."

    monkeypatch.setattr("core.agents.responses.load_response", respuesta)
    veredicto = _verificar("El total es $500.000.", [COTIZACION])
    assert await instruccion_correctiva(None, veredicto) == "Usa solo estos: $600.000."


@pytest.mark.asyncio
async def test_instruccion_cae_al_texto_generico_sin_seccion(monkeypatch) -> None:
    async def sin_seccion(_spec, _clave):
        return None

    monkeypatch.setattr("core.agents.responses.load_response", sin_seccion)
    veredicto = _verificar("Tu pedido quedó creado.", [])
    assert "no verificada" in await instruccion_correctiva(None, veredicto)


# ---------------------------------------------------------------- cierres
def test_cierre_sin_accion_falla_con_status_sin_evidencia() -> None:
    veredicto = _verificar("Listo, tu pedido quedó creado.", [])
    assert not veredicto.passed
    assert veredicto.motivo == "status_sin_evidencia"
    assert veredicto.afirmacion == "pedido"


def test_cierre_con_la_accion_fallida_tampoco_se_respalda() -> None:
    fallida = _tool("crear_pedido", ok=False, error="Falta la dirección.")
    assert not _verificar("Listo, tu pedido quedó creado.", [fallida]).passed


def test_cierre_de_un_turno_anterior_no_respalda() -> None:
    anterior = _tool("crear_pedido", actual=False, status="confirmed")
    assert not _verificar("Listo, tu pedido quedó creado.", [anterior]).passed


def test_cierre_con_la_accion_del_turno_pasa() -> None:
    creado = _tool("crear_pedido", status="confirmed", public_order_number="OC-0003")
    assert _verificar("¡Listo! Tu pedido OC-0003 quedó confirmado.", [creado]).passed


@pytest.mark.parametrize(
    ("texto", "resultados"),
    [
        # Falsos positivos medidos en los journeys (2026-10-09).
        (
            "Listo, Ana. Te resumo:\n\nPlan Basic (mensual): $200,000\nTotal: $200,000\n\n"
            "¿Confirmás el total de $200,000 para generar tu pedido y el link de pago?",
            [_tool("generar_cotizacion", total=200000, status="draft")],
        ),
        (
            "Para dejarte el link de pago necesito armar primero tu pedido. "
            "¿Me confirmás tu nombre completo y tu email?",
            [],
        ),
        (
            "Tenés razón, me adelanté. Todavía no generé ningún link de pago: "
            "para eso primero necesito armar tu pedido.",
            [],
        ),
        (
            "Todavía no generé el link, porque primero necesito tu confirmación del total. "
            "Con tu sí, te envío el link de pago en línea.",
            [],
        ),
        (
            "Listo, el cambio al Plan Premium quedó programado.\n\nPara activarlo hay que pagar la "
            "diferencia prorrateada de $400.000. Te dejo el link de pago:\n\n"
            "https://pagos.simulados/sub-7012abb2\n\nApenas se confirme el pago, tu plan cambia a Premium.",
            [_tool("sub_cambiar_plan", diferencia={"monto": 400000.0, "url": "https://pagos.simulados/sub-7012abb2"})],
        ),
        # Una pregunta sin "¿" inicial tampoco afirma.
        ("Confirmás el pedido y te genero el link de pago?", []),
    ],
)
def test_preguntas_negaciones_y_condicionales_no_afirman(texto: str, resultados: list[dict]) -> None:
    veredicto = _verificar(texto, resultados)
    assert veredicto.passed, veredicto


@pytest.mark.parametrize(
    "texto",
    [
        # Incidente real (yopoldo, 2026-09-11) y su variante del mismo día.
        "¡Listo, Robinson! Cerré tu pedido. 🎉\n\nAhora te envío el link de pago para que lo "
        "hagas en línea.\n\nEn un momentico el asesor te pasa el link y queda todo confirmado.",
        "¡Listo, Robinson! Cerramos tu pedido del Pollo Broaster. 🎉\n\n"
        "Ahora mismo te genero el link de pago para que lo hagas en línea.",
        "Te envío el link de pago en un momento.",
        # La negación de otra cláusula no salva la afirmación.
        "No te preocupes, tu pedido quedó confirmado.",
    ],
)
def test_promesas_sin_accion_siguen_fallando(texto: str) -> None:
    assert not _verificar(texto, []).passed


def test_link_de_pago_lo_respalda_cualquier_url_del_turno() -> None:
    resultado = _tool("sub_link_pago", url="https://pagos.simulados/sub-1", monto=90000.0)
    texto = "Tu cargo es de $90.000. Aquí tienes el link de pago: https://pagos.simulados/sub-1"
    assert _verificar(texto, [resultado]).passed


def test_una_imagen_no_respalda_el_link_de_pago() -> None:
    catalogo = _tool("consultar_inventario", matches=[{"image_url": "https://cdn/x.jpg", "price": 1000}])
    assert not _verificar("Te envío el link de pago ahora mismo.", [catalogo]).passed


# ---------------------------------------------------------------- links
def test_link_inventado_se_quita_sin_reintento() -> None:
    creado = _tool("crear_pedido", status="confirmed", _safe_append="Paga aquí: https://checkout.wompi.co/l/real")
    veredicto = _verificar(
        "Tu pedido quedó confirmado. Paga en https://checkout.wompi.co/l/inventado.", [creado]
    )
    assert veredicto.passed
    assert veredicto.urls_quitadas == 1
    assert "inventado" not in veredicto.texto
    assert veredicto.texto == "Tu pedido quedó confirmado. Paga en."


def test_link_del_servidor_se_conserva() -> None:
    creado = _tool("confirmar_pago_pedido", url="https://checkout.wompi.co/l/real")
    veredicto = _verificar("Aquí está tu link de pago: https://checkout.wompi.co/l/real.", [creado])
    assert veredicto.passed and veredicto.urls_quitadas == 0


def test_link_del_negocio_en_el_prompt_se_conserva() -> None:
    contexto = "## Identidad\nNuestra web es https://minegocio.com y el menú https://minegocio.com/menu"
    veredicto = _verificar("Mira el menú en https://minegocio.com/menu.", [], contexto)
    assert veredicto.urls_quitadas == 0


def test_link_de_pago_de_un_turno_anterior_se_quita() -> None:
    """ADR-0087: el link pudo quedar vencido; "te repito el link" exige pedirlo de nuevo."""
    anterior = _tool("confirmar_pago_pedido", actual=False, url="https://checkout.wompi.co/l/viejo")
    veredicto = _verificar("Este es: https://checkout.wompi.co/l/viejo", [anterior])
    assert veredicto.urls_quitadas == 1


def test_links_de_una_tool_que_termino_con_error_se_conservan_y_uno_inventado_se_quita() -> None:
    """ADR-0125: `terminos_pendientes` trae `error` (el pedido no se creó), pero sus enlaces
    legales los dio el servidor: tienen que llegarle al cliente. Una URL inventada se sigue
    quitando, y la tool fallida no respalda un cierre ni un link de pago."""
    documentos = [
        {"tipo": t, "version": "v1", "url": f"https://solvershub.test/legal/{t}"}
        for t in ("terminos", "privacidad", "dpa")
    ]
    frenado = _tool(
        "crear_pedido", ok=False, status="terminos_pendientes", error="Falta aceptar los términos.",
        documentos=documentos,
    )
    enlaces = "\n".join(d["url"] for d in documentos)
    veredicto = _verificar(f"Antes de crear el pedido, acepta estos documentos:\n{enlaces}\n¿Los aceptas?", [frenado])
    assert veredicto.passed and veredicto.urls_quitadas == 0
    assert all(d["url"] in veredicto.texto for d in documentos)

    inventado = _verificar(f"Acepta estos: {enlaces}\ny este https://solvershub.test/legal/otro", [frenado])
    assert inventado.urls_quitadas == 1 and "legal/otro" not in inventado.texto
    assert not _verificar("Listo, tu pedido quedó confirmado.", [frenado]).passed
    assert not _verificar("Te envío el link de pago.", [frenado]).passed


def test_una_url_que_la_tool_fallida_repite_en_su_error_se_quita() -> None:
    fallida = _tool("crear_pedido", ok=False, error="No existe https://falsa.co/x", hint="Revisa https://falsa.co/y")
    veredicto = _verificar("Mira https://falsa.co/x y https://falsa.co/y", [fallida])
    assert veredicto.urls_quitadas == 2


def test_un_link_de_pago_de_una_tool_fallida_no_respalda_el_link_de_pago() -> None:
    fallida = _tool("confirmar_pago_pedido", ok=False, error="La pasarela no respondió.", url="https://pagos/x")
    assert not _verificar("Te envío el link de pago.", [fallida]).passed


# ---------------------------------------------------------------- configuración
def test_interruptores_del_negocio() -> None:
    assert _verificar("Tu pedido quedó creado.", [], desactivados={"verificador_cierre"}).passed
    assert _verificar("Son $1.000.", [], desactivados={"verificador_montos"}).passed
    veredicto = _verificar("Ver https://x.com/a", [], desactivados={"verificador_links"})
    assert veredicto.urls_quitadas == 0


def test_normalizar_traduce_los_nombres_viejos() -> None:
    assert normalizar_desactivados(["crm_amount_check", "crm_completion_check", "cob_promesa_check"]) == [
        "cob_promesa_check",
        "verificador_cierre",
        "verificador_montos",
    ]
    assert normalizar_desactivados(None) == []
    assert set(TOGGLES) == {"verificador_cierre", "verificador_links", "verificador_montos"}


# ---------------------------------------------------------------- modo sombra y robustez
def test_referencias_y_fechas_se_cuentan_sin_bloquear() -> None:
    veredicto = _verificar("Tu pedido OC-0099 sale el 15 de octubre a las 3:00.", [_tool("crear_pedido")])
    assert veredicto.passed
    assert veredicto.conteo["referencias"] == 1 and veredicto.conteo["fechas"] == 2
    assert veredicto.sombra["referencias_sin_evidencia"] == 1


def test_un_error_del_verificador_no_tumba_el_turno() -> None:
    class Rota:
        def finditer(self, _texto):
            raise RuntimeError("patrón roto")

    rota = Afirmacion(label="rota", patron=Rota())  # type: ignore[arg-type]
    veredicto = verificar_seguro("Tu pedido quedó creado. Paga en https://x.co/a", [], "", [rota])
    assert veredicto.passed and veredicto.error
    # Montos y cierres pasan, pero ningún link sale si el verificador se rompió.
    assert veredicto.texto == "Tu pedido quedó creado. Paga en"


def test_cifras_absurdas_y_nan_no_rompen_la_verificacion() -> None:
    from core.shared.dinero import parse_money

    assert parse_money("$1" + "0" * 400) is None
    resultados = [_tool("rara", price=float("nan"), total=float("inf"), subtotal=True)]
    veredicto = verificar_seguro("Son $1" + "0" * 400 + ". Mira https://x.co/b", resultados, "", AFIRMACIONES)
    assert not veredicto.error
    assert veredicto.urls_quitadas == 1


def test_urls_con_www_y_mayusculas() -> None:
    respaldo = _tool("confirmar_pago_pedido", url="https://Tienda.com/pago/")
    veredicto = _verificar("Paga en HTTPS://tienda.com/pago o en www.tienda.com/pago.", [respaldo])
    assert veredicto.urls_quitadas == 0
    veredicto = _verificar("Paga en www.estafa.com/pago y [www.otra.co](https://otra.co).", [respaldo])
    assert veredicto.urls_quitadas == 3
    assert "estafa" not in veredicto.texto and "otra" not in veredicto.texto


def test_quitar_una_url_no_rearma_otra() -> None:
    veredicto = _verificar("https://a.co y httpshttps://a.co://evil.com", [])
    from core.orchestrator.verificador.evidencia import urls_en

    assert "evil" not in veredicto.texto and urls_en(veredicto.texto) == []


def test_la_pagina_de_un_producto_no_respalda_el_link_de_pago() -> None:
    catalogo = _tool("consultar_inventario", matches=[{"name": "Plan", "price": 1000, "ficha": "https://t.co/plan"}])
    assert not _verificar("Te envío el link de pago ahora mismo.", [catalogo]).passed


def test_rachas_de_espacios_no_bloquean() -> None:
    import time

    inicio = time.perf_counter()
    _verificar("cerré" + " " * 50_000 + "x", [])
    assert time.perf_counter() - inicio < 1.0


# ---------------------------------------------------------------- host
@pytest.mark.asyncio
async def test_solo_cargan_las_afirmaciones_de_packs_habilitados(monkeypatch) -> None:
    from core.mcp.host import MCPHost
    from core.orchestrator.verificador import cargar_afirmaciones

    host = MCPHost()
    for plugin_id, afirmacion in (("pack_x", PEDIDO), ("pack_y", LINK)):

        async def fuente(a=afirmacion):
            return [a]

        host.begin_activation(plugin_id)
        host.register_afirmaciones(fuente)
        host.end_activation()
    monkeypatch.setattr("core.mcp.host.mcp_host", host)

    assert [a.label for a in await cargar_afirmaciones({"pack_x"})] == ["pedido"]
    host.remove_plugin("pack_x")
    assert await cargar_afirmaciones({"pack_x"}) == []


@pytest.mark.parametrize("nombre", ["verificador_montos", "verificador_cierre", "crm_amount_check"])
def test_un_pack_no_puede_registrar_un_nombre_del_verificador(nombre: str) -> None:
    from core.mcp.host import MCPHost
    from core.mcp.types import GuardrailResult
    from core.shared.exceptions import PluginError

    async def guardrail(_texto, _resultados):
        return GuardrailResult(passed=True)

    host = MCPHost()
    host.begin_activation("pack_x")
    with pytest.raises(PluginError):
        host.register_guardrail(nombre, guardrail)


def test_cotizacion_generada_solo_la_respalda_generar_cotizacion() -> None:
    """`cotizar_plan` se retiró de Comercio (HU-COM-17): solo `generar_cotizacion` respalda el aviso."""
    from pack_commerce.module import COMPLETION_CLAIMS

    patron, tools = COMPLETION_CLAIMS["cotizacion_generada"]
    afirmacion = Afirmacion(label="cotizacion_generada", patron=patron, tools=frozenset(tools))
    texto = "Listo, te generé la cotización del Plan Premium mensual."
    con = verificar(texto, Evidencia.desde([{"_tool_name": "generar_cotizacion", "total": 600000}]), [afirmacion])
    assert con.passed, con.motivo
    sin = verificar(texto, Evidencia.desde([{"_tool_name": "cotizar_plan", "total": 600000}]), [afirmacion])
    assert not sin.passed and sin.motivo == "status_sin_evidencia"
    sin = verificar(texto, Evidencia.desde([{"_tool_name": "consultar_inventario"}]), [afirmacion])
    assert not sin.passed and sin.motivo == "status_sin_evidencia"
