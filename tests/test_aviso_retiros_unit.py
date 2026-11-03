"""HU-AOS-59 · Aviso en palabras simples de las referencias retiradas (ADR-0184). Sin BD ni red.

El texto lo leen el dueño (editor y publicar) y el master: tiene que decir qué ya no existe y qué
hacer, sin los códigos internos del detector. El binario es uno armado a mano y el
contenido sale de `tests/fixtures/agents/`, que todavía nombra lo que SPEC-43 PR-4 retiró.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.agents.aviso_retiros import (
    CODIGO_AVISO,
    avisos_de_contenido,
    problemas_de_publicacion,
    texto_de,
    tipo_api,
    tipo_de,
)
from core.agents.referencias_retiradas import (
    CODIGO_ACCION,
    CODIGO_PROCEDIMIENTO,
    Binario,
)

FIXTURES = Path(__file__).parent / "fixtures" / "agents"
VIEJO = "pack_commerce.contratar_plan"
NUEVO = "pack_subscriptions.contratar_plan"
CODIGOS = ("accion_inexistente", "procedimiento_inexistente", CODIGO_AVISO)


@pytest.fixture(scope="module")
def binario() -> Binario:
    """El binario de hoy, armado a mano: trae las acciones comunes de ventas y ningún procedimiento de
    comercio, así que `informar_plan` y `contratar_plan` están retirados sin depender de los packs instalados."""
    return Binario(acciones=frozenset({"consultar_inventario", "check_availability", "cancel_calendar_event"}))


def _ventas() -> dict[str, Any]:
    """El agente del incidente: el procedimiento retirado en `procedures[0]` e `informar_plan` en `tools[3]`."""
    spec = json.loads((FIXTURES / "ventas.agentspec.json").read_text(encoding="utf-8"))
    spec["procedures"] = [spec["procedures"][0]]
    spec["tools"] = [
        {"tool": nombre, "enabled": True, "scopes": []}
        for nombre in ("consultar_inventario", "check_availability", "cancel_calendar_event", "informar_plan")
    ]
    return spec


# ---------------------------------------------------------------- el texto
def test_accion_retirada_dice_que_ya_no_existe_y_donde_quitarla() -> None:
    texto = texto_de("tools[3].tool", "accion", "informar_plan")
    assert "«informar_plan» ya no existe" in texto
    assert "herramientas del agente" in texto
    assert "vuelve a publicar" in texto
    assert "avisa al equipo" not in texto  # tiene entrada en el registro: es un retiro declarado


def test_procedimiento_con_reemplazo_dice_como_se_llama_ahora() -> None:
    texto = texto_de("procedures[0].id", "procedimiento", VIEJO)
    assert f"«{VIEJO}» ya no existe" in texto
    assert f"ahora se llama «{NUEVO}»" in texto
    assert "habilidad" in texto  # el reemplazo solo vale con la habilidad activa


def test_nombre_sin_entrada_en_el_registro_pide_avisar_al_equipo() -> None:
    accion = texto_de("tools[0].tool", "accion", "accion_que_nunca_estuvo")
    proc = texto_de("procedures[0].id", "procedimiento", "pack_x.nunca_estuvo")
    assert "avisa al equipo" in accion and "avisa al equipo" in proc
    assert "reemplazo conocido" in proc


def test_lo_que_corre_hoy_lo_dice_distinto_a_lo_que_se_edita() -> None:
    en_uso = texto_de("components[0].procedures[1].id", "procedimiento", VIEJO)
    assert "todavía lo usa" in en_uso
    assert "todavía lo usa" not in texto_de("procedures[0].id", "procedimiento", VIEJO)


@pytest.mark.parametrize(
    ("ruta", "donde"),
    [
        ("tools[0].tool", "herramientas del agente"),
        ("guardrails[2].tool", "reglas del agente"),
        ("exposes.consultas[1].action", "consultas que el agente ofrece"),
        ("procedures[0].id", "procedimientos del agente"),
        ("routing.rules[4].start", "reglas de ruteo"),
    ],
)
def test_cada_ruta_se_nombra_en_palabras_del_editor(ruta: str, donde: str) -> None:
    tipo = "procedimiento" if ruta.startswith(("procedures", "routing")) else "accion"
    texto = texto_de(ruta, tipo, "x" if tipo == "accion" else "pack_x.y")
    assert donde in texto
    assert ruta not in texto  # la ruta técnica va aparte, en `field`


def test_ningun_texto_lleva_codigos_internos(binario: Binario) -> None:
    textos = [t for *_, t in avisos_de_contenido(_ventas(), binario)]
    textos += [texto_de(r, t, n) for r in ("tools[0].tool", "components[0].procedures[0].id") for t in ("accion", "procedimiento") for n in ("informar_plan", VIEJO, "otro")]
    assert textos
    for texto in textos:
        assert not any(c in texto for c in CODIGOS), texto


# ---------------------------------------------------------------- el contenido
def test_el_aviso_del_contenido_marca_el_campo_el_tipo_y_el_nombre(binario: Binario) -> None:
    avisos = {campo: (tipo, nombre) for campo, tipo, nombre, _ in avisos_de_contenido(_ventas(), binario)}
    assert avisos == {
        "tools[3].tool": ("action", "informar_plan"),
        "procedures[0].id": ("procedure", VIEJO),
    }


def test_un_contenido_limpio_no_avisa(binario: Binario) -> None:
    limpio = _ventas()
    limpio["tools"] = limpio["tools"][:3]
    limpio["procedures"] = []
    assert avisos_de_contenido(limpio, binario) == []
    assert problemas_de_publicacion(limpio, binario) == []


def test_publicar_recibe_un_problema_por_referencia_con_el_mismo_texto(binario: Binario) -> None:
    problemas = problemas_de_publicacion(_ventas(), binario)
    assert {p.path for p in problemas} == {"tools[3].tool", "procedures[0].id"}
    assert {p.code for p in problemas} == {CODIGO_AVISO}
    por_campo = {campo: texto for campo, _t, _n, texto in avisos_de_contenido(_ventas(), binario)}
    assert {p.path: p.message for p in problemas} == por_campo


def test_el_aviso_no_inventa_lo_que_el_detector_no_ve() -> None:
    """Un procedimiento abierto (`=0.0.0`) lo deriva el flujo del negocio, no el binario: no se avisa."""
    binario = Binario(acciones=frozenset({"consultar_inventario"}))
    contenido = {"contract": "agentspec/3", "tools": [], "procedures": [{"id": VIEJO, "version": "=0.0.0"}]}
    assert problemas_de_publicacion(contenido, binario) == []


def test_tipos_de_los_codigos_del_detector() -> None:
    assert tipo_de(CODIGO_ACCION) == "accion" and tipo_api(tipo_de(CODIGO_ACCION)) == "action"
    assert tipo_de(CODIGO_PROCEDIMIENTO) == "procedimiento" and tipo_api(tipo_de(CODIGO_PROCEDIMIENTO)) == "procedure"


# ---------------------------------------------------------------- el 422 de publicar
def test_publicar_con_una_referencia_retirada_da_422_con_el_texto_simple(binario: Binario) -> None:
    from core.api.routers.agents import _contrato_422

    contenido = _ventas()
    contenido["procedures"] = []
    (problema,) = problemas_de_publicacion(contenido, binario)
    error = _contrato_422([problema])
    assert error.status_code == 422 and error.code == "invalid_spec"  # el mismo status y código de antes
    assert error.message == problema.message
    assert error.fields == {"tools[3].tool": problema.message}
    assert error.details == {"retired_references": [{"field": "tools[3].tool", "message": problema.message}]}
    assert not any(c in error.message for c in CODIGOS)


def test_publicar_con_varias_da_un_resumen_y_el_texto_de_cada_una(binario: Binario) -> None:
    from core.api.routers.agents import _contrato_422

    problemas = problemas_de_publicacion(_ventas(), binario)
    error = _contrato_422(problemas)
    assert error.status_code == 422 and error.code == "invalid_spec"
    assert "2 cosas que ya no existen" in error.message
    assert error.fields == {p.path: p.message for p in problemas}
    assert error.details is not None and len(error.details["retired_references"]) == 2


def test_un_problema_comun_del_contrato_sigue_igual() -> None:
    from core.api.routers.agents import _contrato_422
    from core.kernel.procedures import Issue

    error = _contrato_422([Issue("binding_repetido", "tools", "La acción 'x' tiene más de un binding.")])
    assert error.status_code == 422 and error.code == "invalid_spec"
    assert error.message == "El spec tiene 1 problema(s) del contrato."
    assert error.fields == {"tools": "La acción 'x' tiene más de un binding."}
    assert error.details is None
