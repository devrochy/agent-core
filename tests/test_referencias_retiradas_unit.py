"""HU-AOS-59 · Detector y reescritor puros de referencias retiradas (ADR-0184).

Sin BD ni red. El binario es el real (`plugin_manager.scan()`) salvo donde la prueba arma uno
propio; los contenidos salen de `tests/fixtures/agents/`, que todavía nombran el procedimiento que
SPEC-43 PR-4 retiró (`pack_commerce.contratar_plan`): el caso del incidente.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from core.agents.contrato_v3 import lint_agentspec, revisar_spec, validar_spec
from core.agents.referencias_retiradas import (
    CODIGO_ACCION,
    CODIGO_PROCEDIMIENTO,
    Binario,
    binario_de,
    hallazgos_de_contenido,
    migrar_contenido,
)
from core.agents.retiros import Reemplazo
from core.kernel.procedures import ActionInfo, catalogo_de_acciones
from core.plugin_manager import plugin_manager

FIXTURES = Path(__file__).parent / "fixtures" / "agents"
NUEVO = "pack_subscriptions.contratar_plan"
VIEJO = "pack_commerce.contratar_plan"


def _fixture(nombre: str) -> dict[str, Any]:
    return json.loads((FIXTURES / nombre).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def modulos() -> list[Any]:
    return plugin_manager.scan()


@pytest.fixture(scope="module")
def binario(modulos: list[Any]) -> Binario:
    return binario_de(modulos)


@pytest.fixture(scope="module")
def catalogo(modulos: list[Any]) -> Mapping[str, ActionInfo]:
    return catalogo_de_acciones(modulos)


def _ventas() -> dict[str, Any]:
    """El agente de ventas del incidente: el procedimiento retirado (con textos propios), `informar_plan`
    en el cuarto binding y lo demás con nombres que el binario sí trae."""
    spec = _fixture("ventas.agentspec.json")
    spec["procedures"] = [spec["procedures"][0]]  # pack_commerce.contratar_plan ^1.0, con overrides
    spec["tools"] = [
        {"tool": nombre, "enabled": True, "scopes": []}
        for nombre in ("consultar_inventario", "check_availability", "cancel_calendar_event", "informar_plan")
    ]
    return spec


def _siempre(_: Reemplazo) -> None:
    return None


def _nunca(_: Reemplazo) -> str:
    return "la habilidad 'pack_subscriptions' no está en el negocio"


# ------------------------------------------------------------------ el binario de hoy
def test_el_binario_ya_no_trae_lo_retirado_y_si_su_reemplazo(binario: Binario) -> None:
    assert "informar_plan" not in binario.acciones
    assert not binario.tiene_procedimiento(VIEJO)
    assert binario.tiene_procedimiento(NUEVO)


# ------------------------------------------------------------------ detección
def test_detecta_la_accion_y_el_procedimiento_retirados_con_su_ruta(binario: Binario) -> None:
    spec = _ventas()
    hallazgos = {(h.ruta, h.codigo, h.nombre): h for h in hallazgos_de_contenido(spec, binario)}
    assert set(hallazgos) == {
        ("tools[3].tool", CODIGO_ACCION, "informar_plan"),
        ("procedures[0].id", CODIGO_PROCEDIMIENTO, VIEJO),
    }
    assert hallazgos[("tools[3].tool", CODIGO_ACCION, "informar_plan")].destino == "quitar"
    assert hallazgos[("procedures[0].id", CODIGO_PROCEDIMIENTO, VIEJO)].destino == f"reemplazar por {NUEVO} ^2.0"


def test_un_contenido_limpio_o_que_no_es_un_objeto_no_da_hallazgos(binario: Binario) -> None:
    limpio = _ventas()
    limpio["tools"] = limpio["tools"][:3]
    limpio["procedures"] = [{"id": NUEVO, "version": "^2.0"}]
    limpio["exposes"]["procedures"] = [NUEVO]
    assert hallazgos_de_contenido(limpio, binario) == []
    assert hallazgos_de_contenido(None, binario) == []
    assert hallazgos_de_contenido([], binario) == []
    assert hallazgos_de_contenido({"contract": "agentspec/3", "tools": "no-es-lista"}, binario) == []


def test_una_declarativa_con_ese_nombre_no_es_una_accion_retirada(binario: Binario) -> None:
    spec = _ventas()
    spec["declarativeTools"][0]["name"] = "informar_plan"
    assert all(h.nombre != "informar_plan" for h in hallazgos_de_contenido(spec, binario))


def test_un_procedimiento_abierto_no_se_compara_con_el_binario(binario: Binario) -> None:
    spec = _ventas()
    spec["tools"] = spec["tools"][:3]
    spec["procedures"] = [{"id": "tenant.planes_del_negocio", "version": "=0.0.0"}]
    assert hallazgos_de_contenido(spec, binario) == []


def test_detecta_la_regla_obligatoria_y_la_consulta_que_dependen_de_una_accion_retirada(binario: Binario) -> None:
    spec = _ventas()
    spec["guardrails"].append(
        {"name": "exige_plan", "kind": "required_tool", "action": "retry", "phrases": ["plan"], "tool": "informar_plan"}
    )
    spec["exposes"]["consultas"].append({"id": "ventas.plan", "action": "informar_plan", "outputSchema": {"type": "object"}})
    rutas = {h.ruta for h in hallazgos_de_contenido(spec, binario)}
    assert {"guardrails[1].tool", "exposes.consultas[1].action"} <= rutas


def test_un_nombre_desaparecido_sin_entrada_se_detecta_pero_no_tiene_retiro(binario: Binario) -> None:
    spec = _ventas()
    spec["tools"].append({"tool": "accion_que_nadie_registro", "enabled": True, "scopes": []})
    (h,) = [h for h in hallazgos_de_contenido(spec, binario) if h.nombre == "accion_que_nadie_registro"]
    assert h.retiro is None and "sin entrada" in h.destino


def test_misma_regla_que_el_linter_de_publicacion_para_acciones(binario: Binario, catalogo: Mapping[str, ActionInfo]) -> None:
    spec = _ventas()
    spec["guardrails"].append(
        {"name": "exige_plan", "kind": "required_tool", "action": "retry", "phrases": ["plan"], "tool": "informar_plan"}
    )
    modelo, problemas = validar_spec(spec, "agentspec/3")
    assert modelo is not None, problemas
    del_linter = {i.path for i in lint_agentspec(modelo, catalogo) if i.code == CODIGO_ACCION}  # type: ignore[arg-type]
    mias = {h.ruta for h in hallazgos_de_contenido(spec, binario) if h.codigo == CODIGO_ACCION}
    assert mias == del_linter == {"tools[3].tool", "guardrails[1].tool"}


def test_en_el_kernelspec_solo_marca_lo_que_el_registro_conoce(binario: Binario) -> None:
    kernel = _fixture("coordinador.kernelspec.json")
    assert [(h.ruta, h.nombre) for h in hallazgos_de_contenido(kernel, binario)] == [("routing.rules[0].start", VIEJO)]
    kernel["routing"]["rules"][0]["start"] = "tenant.abierto_del_flujo"
    assert hallazgos_de_contenido(kernel, binario) == []


# ------------------------------------------------------------------ migración
def test_migra_con_el_reemplazo_declarado_y_el_resultado_pasa_el_contrato(
    binario: Binario, catalogo: Mapping[str, ActionInfo]
) -> None:
    original = _ventas()
    copia = copy.deepcopy(original)
    m = migrar_contenido(original, binario, _siempre)
    assert original == copia  # no toca el original
    assert [t["tool"] for t in m.contenido["tools"]] == ["consultar_inventario", "check_availability", "cancel_calendar_event"]
    assert m.contenido["procedures"][0] == {"id": NUEVO, "version": "^2.0"}
    assert m.contenido["exposes"]["procedures"] == [NUEVO]
    assert m.cambia and m.pendientes == []
    assert any("overrides" in a for a in m.avisos)  # el original traía textos propios para el viejo
    assert revisar_spec(m.contenido, catalogo, "agentspec/3") == []
    # Idempotente: una segunda pasada ya no encuentra nada.
    assert hallazgos_de_contenido(m.contenido, binario) == []
    assert not migrar_contenido(m.contenido, binario, _siempre).cambia


def test_sin_la_habilidad_quita_lo_retirado_y_no_agrega_el_reemplazo(
    binario: Binario, catalogo: Mapping[str, ActionInfo]
) -> None:
    m = migrar_contenido(_ventas(), binario, _nunca)
    ids = [p["id"] for p in m.contenido["procedures"]]
    assert VIEJO not in ids and NUEVO not in ids
    assert m.contenido["exposes"]["procedures"] == []  # no expone lo que ya no ejecuta
    assert any("no se agregó" in a and "pack_subscriptions" in a for a in m.avisos)
    assert revisar_spec(m.contenido, catalogo, "agentspec/3") == []


def test_si_ya_tenia_el_procedimiento_nuevo_no_lo_duplica(binario: Binario, catalogo: Mapping[str, ActionInfo]) -> None:
    spec = _ventas()
    spec["procedures"].append({"id": NUEVO, "version": "^2.0"})
    m = migrar_contenido(spec, binario, _siempre)
    ids = [p["id"] for p in m.contenido["procedures"]]
    assert ids.count(NUEVO) == 1 and VIEJO not in ids
    assert revisar_spec(m.contenido, catalogo, "agentspec/3") == []


def test_quita_la_regla_obligatoria_y_desliga_la_consulta_de_una_accion_retirada(
    binario: Binario, catalogo: Mapping[str, ActionInfo]
) -> None:
    spec = _ventas()
    spec["guardrails"].append(
        {"name": "exige_plan", "kind": "required_tool", "action": "retry", "phrases": ["plan"], "tool": "informar_plan"}
    )
    spec["exposes"]["consultas"].append({"id": "ventas.plan", "action": "informar_plan", "outputSchema": {"type": "object"}})
    m = migrar_contenido(spec, binario, _siempre)
    assert [r["name"] for r in m.contenido["guardrails"]] == ["sin_promesas"]
    assert m.contenido["exposes"]["consultas"][1]["action"] is None
    assert any("ventas.plan" in a for a in m.avisos)
    assert revisar_spec(m.contenido, catalogo, "agentspec/3") == []


def test_lo_que_no_esta_en_el_registro_queda_pendiente_y_no_se_toca(binario: Binario) -> None:
    spec = _ventas()
    spec["tools"].append({"tool": "accion_que_nadie_registro", "enabled": True, "scopes": []})
    m = migrar_contenido(spec, binario, _siempre)
    assert [h.nombre for h in m.pendientes] == ["accion_que_nadie_registro"]
    assert "accion_que_nadie_registro" in [t["tool"] for t in m.contenido["tools"]]


def test_migra_el_kernelspec_cambiando_el_inicio_de_la_regla_o_quitandola(
    binario: Binario, catalogo: Mapping[str, ActionInfo]
) -> None:
    kernel = _fixture("coordinador.kernelspec.json")
    con = migrar_contenido(kernel, binario, _siempre)
    assert con.contenido["routing"]["rules"][0]["start"] == NUEVO
    assert revisar_spec(con.contenido, catalogo, "kernelspec/1") == []
    sin = migrar_contenido(kernel, binario, _nunca)
    assert [r["id"] for r in sin.contenido["routing"]["rules"]] == ["cliente_en_mora", "noche"]
    assert any("no se agregó" in a for a in sin.avisos)
    assert revisar_spec(sin.contenido, catalogo, "kernelspec/1") == []


def test_una_etapa_del_flujo_del_procedimiento_retirado_quita_la_regla(binario: Binario) -> None:
    kernel = _fixture("coordinador.kernelspec.json")
    kernel["routing"]["rules"].append(
        {"id": "en_etapa", "when": {"flowStage": {"procedure": VIEJO, "stage": "pago"}}, "agent": "ventas"}
    )
    m = migrar_contenido(kernel, binario, _siempre)
    assert "en_etapa" not in [r["id"] for r in m.contenido["routing"]["rules"]]
