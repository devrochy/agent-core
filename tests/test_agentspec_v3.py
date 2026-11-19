"""HU-AOS-12 · contratos `agentspec/3` y `kernelspec/1`, validador y linter (ADR-0113).

Unitarios, sin BD ni red. El catálogo de acciones sale del binario real
(`plugin_manager.scan()`), como en `tests/test_procedures.py`. Los fixtures de
`tests/fixtures/agents/` también los corre el CI con `agent-core agent lint`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from agentcore_contracts import (
    AgentSpec,
    AgentSpecV3,
    KernelSpec,
    SecretRef,
    __version__,
)
from pydantic import ValidationError
from typer.testing import CliRunner

from core.agents.contrato_v3 import revisar_spec, validar_spec
from core.kernel.procedures import ActionInfo, catalogo_de_acciones
from core.plugin_manager import plugin_manager

FIXTURES = Path(__file__).parent / "fixtures" / "agents"
REPO = Path(__file__).resolve().parent.parent


def fixture(nombre: str) -> dict[str, Any]:
    return json.loads((FIXTURES / nombre).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalogo() -> Mapping[str, ActionInfo]:
    return catalogo_de_acciones(plugin_manager.scan())


def codes(issues: list[Any]) -> list[str]:
    return [i.code for i in issues]


# ------------------------------------------------------------------ paquete
def test_major_3_y_sin_espejo_ts() -> None:
    # 3.1.0: `knowledge` opcional (ADR-0145, adenda de ADR-0113 del 2026-10-20).
    assert __version__ == "3.2.0"
    assert not (REPO / "packages" / "contracts" / "src").exists()
    assert not (REPO / "packages" / "contracts" / "package.json").exists()
    # `AgentSpec` sigue siendo el v2: lo leen el loop legacy, los presets y el alta.
    assert {"packs", "extensions", "tenantId"} <= set(AgentSpec.model_fields)
    assert AgentSpecV3.model_fields["contract"].default == "agentspec/3"
    assert KernelSpec.model_fields["contract"].default == "kernelspec/1"


@pytest.mark.parametrize("nombre", ["ventas.agentspec.json", "coordinador.kernelspec.json"])
def test_fixtures_validos(nombre: str, catalogo) -> None:
    assert revisar_spec(fixture(nombre), catalogo) == []


def test_identidad_y_kind_no_viven_en_el_contrato() -> None:
    """`kind` vive en `agents` y la identidad la da la fila (ADR-0113 §2)."""
    for campo in ("id", "tenantId", "version", "kind", "key", "packs"):
        raw = fixture("ventas.agentspec.json") | {campo: "x"}
        spec, issues = validar_spec(raw)
        assert spec is None
        assert issues[0].path == campo
        assert issues[0].message == "Campo desconocido: agentspec/3 no lo define."


# ------------------------------------------------------------------ secretos
@pytest.mark.parametrize("header", ["Authorization", "authorization", "Cookie", "X-Api-Key", "x-api-key"])
def test_header_sensible_literal_se_rechaza(header: str) -> None:
    """Gherkin «Tools declarativas sin secretos»: un borrador con un literal no se guarda."""
    raw = fixture("ventas.agentspec.json")
    raw["declarativeTools"][0]["headers"] = {header: "Bearer abc123"}
    spec, issues = validar_spec(raw)
    assert spec is None
    assert [i.path for i in issues] == ["declarativeTools[0].headers"]
    assert '{"secret": "<nombre>"}' in issues[0].message
    assert "abc123" not in issues[0].message


def test_header_sensible_por_referencia_y_no_sensible_literal() -> None:
    raw = fixture("ventas.agentspec.json")
    raw["declarativeTools"][0]["headers"] = {"Authorization": {"secret": "token_envios"}, "X-Trace": "1"}
    spec, issues = validar_spec(raw)
    assert issues == [] and isinstance(spec, AgentSpecV3)
    assert spec.declarativeTools[0].headers["Authorization"] == SecretRef(secret="token_envios")
    raw["declarativeTools"][0]["headers"] = {"Authorization": {"secret": "Mayúsculas"}}
    assert validar_spec(raw)[0] is None


# ------------------------------------------------------------------ forma
@pytest.mark.parametrize("rango", ["^1.0", "~1.2.3", "=0.0.0", "1.2.3", "2"])
def test_rangos_de_version_validos(rango: str) -> None:
    raw = fixture("ventas.agentspec.json")
    raw["procedures"][0]["version"] = rango
    assert validar_spec(raw)[1] == []


@pytest.mark.parametrize("rango", [">=1.0", "1.x", "^01.0", "latest", ""])
def test_rangos_de_version_invalidos(rango: str) -> None:
    raw = fixture("ventas.agentspec.json")
    raw["procedures"][0]["version"] = rango
    assert validar_spec(raw)[0] is None


def test_validar_spec_por_contrato_esperado() -> None:
    assert codes(validar_spec(fixture("ventas.agentspec.json"), "kernelspec/1")[1]) == ["contrato"]
    assert codes(validar_spec({"contract": "agentspec/2"})[1]) == ["contrato"]
    assert validar_spec([1])[1][0].message == "El spec debe ser un objeto JSON."
    # Sin `contract`, el esperado decide (la fila del agente fija el contrato por su kind).
    assert isinstance(validar_spec({}, "kernelspec/1")[0], KernelSpec)


def test_kernelspec_reglas_sin_regex_y_un_destino() -> None:
    raw = fixture("coordinador.kernelspec.json")
    sin_condicion = copy.deepcopy(raw)
    sin_condicion["routing"]["rules"][0]["when"] = {}
    _, issues = validar_spec(sin_condicion)
    assert issues[0].path == "routing.rules[0].when"
    assert issues[0].message == "La regla necesita al menos una condición."

    dos_destinos = copy.deepcopy(raw)
    dos_destinos["routing"]["rules"][0]["agent"] = "ventas"
    assert "exactamente uno" in validar_spec(dos_destinos)[1][0].message

    regex = copy.deepcopy(raw)
    regex["routing"]["rules"][0]["when"] = {"regex": "^plan.*"}
    assert validar_spec(regex)[1][0].message == "Campo desconocido: kernelspec/1 no lo define."


def test_kernelspec_defaults_sin_modelo_literal() -> None:
    kernel = KernelSpec()
    assert kernel.routing.defaultAgent == "general"
    assert kernel.routing.coprocessor.model is None
    assert kernel.modelPolicy.highRiskStage.model is None
    assert kernel.policies.budgetUsdPerConversation == 0.5
    assert kernel.policies.backofficeBudgetUsdMonthly == 20
    dump = kernel.model_dump(mode="json", by_alias=True)
    assert KernelSpec.model_validate(dump) == kernel


def test_within_hours_usa_from() -> None:
    raw = fixture("coordinador.kernelspec.json")
    spec, _ = validar_spec(raw)
    assert isinstance(spec, KernelSpec)
    dump = spec.model_dump(mode="json", by_alias=True)
    assert dump["routing"]["rules"][2]["when"]["withinHours"] == {"from": "22:00", "to": "06:00"}


def test_guardrail_campos_por_tipo() -> None:
    raw = fixture("ventas.agentspec.json")
    raw["guardrails"] = [{"name": "x", "kind": "required_tool", "phrases": ["link"]}]
    assert validar_spec(raw)[1][0].message == "Required_tool necesita `tool`."
    with pytest.raises(ValidationError):
        AgentSpecV3.model_validate(raw)


# ------------------------------------------------------------------ linter
def test_linter_agentspec(catalogo) -> None:
    raw = fixture("ventas.agentspec.json")
    raw["tools"].append({"tool": "no_existe_en_el_binario"})
    raw["tools"].append({"tool": "consultar_inventario"})
    raw["declarativeTools"].append(
        {"name": "crear_pedido", "url": "https://a.co/x", "allowedDomains": ["a.co"]}
    )
    raw["declarativeTools"][0]["allowedDomains"].append("localhost")
    raw["guardrails"].append({"name": "g", "kind": "required_tool", "phrases": ["link"], "tool": "fantasma"})
    raw["exposes"]["procedures"].append("pack_commerce.agendar")
    por_codigo = {i.code: i.path for i in revisar_spec(raw, catalogo)}
    assert por_codigo == {
        "binding_repetido": "tools",
        "accion_inexistente": "guardrails[1].tool",
        "declarativa_sombreada": "declarativeTools[1].name",
        "dominio_no_publico": "declarativeTools[0].allowedDomains[1]",
        "expuesto_sin_declarar": "exposes.procedures[1]",
    }


def test_binding_a_declarativa_es_valido(catalogo) -> None:
    raw = fixture("ventas.agentspec.json")
    raw["tools"].append({"tool": "consultar_envio", "requiresConfirmation": True})
    assert revisar_spec(raw, catalogo) == []


def test_linter_kernelspec(catalogo) -> None:
    raw = fixture("coordinador.kernelspec.json")
    raw["routing"]["rules"][1]["id"] = "boton_plan"
    raw["routing"]["rules"][2]["when"]["withinHours"] = {"from": "08:00", "to": "08:00"}
    assert codes(revisar_spec(raw, catalogo)) == ["regla_repetida", "franja_vacia"]


# ------------------------------------------------------------------ CLI
def test_cli_agent_lint(tmp_path: Path) -> None:
    from cli.commands.agent import app

    malo = fixture("ventas.agentspec.json")
    malo["declarativeTools"][0]["headers"] = {"Authorization": "Bearer secreto-literal"}
    archivo = tmp_path / "malo.json"
    archivo.write_text(json.dumps(malo), encoding="utf-8")
    runner = CliRunner()
    ok = runner.invoke(app, ["lint", str(FIXTURES / "ventas.agentspec.json"), str(FIXTURES / "coordinador.kernelspec.json")])
    assert ok.exit_code == 0, ok.output
    mal = runner.invoke(app, ["lint", str(archivo)])
    assert mal.exit_code == 1
    assert "declarativeTools[0].headers" in mal.output
    assert "secreto-literal" not in mal.output


def test_secreto_solo_por_https_y_sin_saltos_de_linea() -> None:
    raw = fixture("ventas.agentspec.json")
    raw["declarativeTools"][0]["url"] = "http://api.transportadora.co/v1/guias/{guia}"
    assert "https" in validar_spec(raw)[1][0].message
    raw = fixture("ventas.agentspec.json")
    raw["declarativeTools"][0]["headers"]["X-Trace"] = "a\r\nX-Inyectado: 1"
    assert validar_spec(raw)[0] is None


@pytest.mark.parametrize("header", ["Proxy-Authorization", "X-Auth-Token", "Api-Key", "Private-Token"])
def test_variantes_de_credencial_tambien_son_sensibles(header: str) -> None:
    raw = fixture("ventas.agentspec.json")
    raw["declarativeTools"][0]["headers"] = {header: "literal"}
    assert validar_spec(raw)[0] is None


def test_documento_muy_anidado_o_con_nan() -> None:
    raw = fixture("ventas.agentspec.json")
    anidado: dict[str, Any] = {}
    cursor = anidado
    for _ in range(40):
        cursor["x"] = {}
        cursor = cursor["x"]
    raw["extensions"] = anidado
    assert "anida" in validar_spec(raw)[1][0].message
    raw = fixture("ventas.agentspec.json")
    raw["extensions"] = {"x": float("nan")}
    assert "no finitos" in validar_spec(raw)[1][0].message
