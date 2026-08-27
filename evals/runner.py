"""Runner de golden scenarios (assertions determinísticas, sin LLM-as-judge).

Uso:  python evals/runner.py            # corre todos los escenarios
      python evals/runner.py 03         # corre solo los que matcheen "03"

Requiere LLM_API_KEY configurada (hace llamadas reales al proveedor activo).
Criterio (heredado de agent-sale ADR-012): assertions duras bloquean; el tono
se revisa manualmente. Correr antes de cada deploy, no en cada commit.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

from agentcore_contracts import AgentSpec

SCENARIOS_DIR = Path(__file__).parent / "golden_scenarios"
SPECS_DIR = Path(__file__).parent / "specs"


def _load_spec(fixture: str = "culto.json") -> AgentSpec:
    """Carga el AgentSpec del tenant (CULTO) desde evals/specs/ (ADR-0016:
    el gate evalúa la configuración real, no un spec sintético)."""
    path = SPECS_DIR / fixture
    if path.exists():
        return AgentSpec.model_validate(json.loads(path.read_text()))
    raise FileNotFoundError(f"Spec fixture no encontrada: {path}")


def _git_ref() -> str:
    try:
        import subprocess

        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


TENANT1 = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _ensure_plugin_active(plugin_id: str) -> bool:
    """Activa un plugin para evals con su config default (si no está activo).

    Corre dentro de tenant_scope: save_config/activate persisten config por
    tenant (RLS + NOT NULL) y sin scope fallarían.
    """
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    module = plugin_manager.get(plugin_id)
    if module is None:
        return False
    async with tenant_scope(TENANT1):
        if not plugin_manager.is_active(plugin_id):
            await plugin_manager.save_config(plugin_id, {})
            await plugin_manager.activate(plugin_id)
        return plugin_manager.is_active(plugin_id)


async def run_scenario(path: Path) -> tuple[str, bool, str]:
    scenario = json.loads(path.read_text())
    name = scenario["name"]
    assertions = scenario.get("assertions", {})

    # Escalamiento por keyword: se evalúa la regla determinística, sin LLM
    if assertions.get("expect_keyword_escalation"):
        from core.orchestrator.escalation import matches_escalation_keywords

        message = scenario["conversation"][-1]["content"]
        ok = matches_escalation_keywords(message)
        return name, ok, "" if ok else "La keyword no disparó escalamiento"

    # Escenarios con LLM real
    from core.config import get_settings
    from core.llm import resolve_provider
    from core.llm.prompt_builder import PromptContext, build_system_blocks
    from core.llm.types import LLMMessage
    from core.mcp.host import mcp_host
    from core.mcp.translator import to_llm_schemas

    settings = get_settings()
    if not settings.llm_api_key:
        return name, False, "LLM_API_KEY no configurada (skip)"

    required = scenario.get("requires_plugins", [])
    for plugin_id in required:
        if not await _ensure_plugin_active(plugin_id):
            return name, True, f"SKIP: requiere {plugin_id} (no instalado)"

    # Spec real del tenant (CULTO) desde evals/specs/ (ADR-0016).
    spec = _load_spec(scenario.get("spec", "culto.json"))
    provider = resolve_provider()
    ctx = PromptContext(
        assistant_name="Asistente de Prueba",
        business_description="Agencia de desarrollo de software y páginas web en Colombia.",
    )
    tools = to_llm_schemas(mcp_host.get_skill_tools([s.name for s in mcp_host.list_skills()]))
    messages = [
        LLMMessage(role=m["role"], content=m["content"]) for m in scenario["conversation"]
    ]
    response = await provider.generate(
        system_blocks=await build_system_blocks(spec, ctx), messages=messages, tools=tools or None
    )

    failures: list[str] = []
    text_lower = response.content.lower()

    for banned in assertions.get("must_not_contain", []):
        if banned.lower() in text_lower:
            failures.append(f"contiene texto prohibido: '{banned}'")

    if assertions.get("must_not_call_tools") and response.tool_calls:
        failures.append(f"llamó tools inesperadas: {[t.name for t in response.tool_calls]}")

    if "must_call_tool" in assertions:
        expected = assertions["must_call_tool"]
        called = [t.name for t in response.tool_calls]
        if expected not in called:
            failures.append(f"no llamó la tool '{expected}' (llamó: {called})")
        elif "tool_arguments_contain" in assertions:
            call = next(t for t in response.tool_calls if t.name == expected)
            for key, expected_fragment in assertions["tool_arguments_contain"].items():
                actual = str(call.arguments.get(key, ""))
                if str(expected_fragment).lower() not in actual.lower():
                    failures.append(f"argumento {key}='{actual}' no contiene '{expected_fragment}'")

    max_chars = assertions.get("max_response_chars")
    if max_chars and len(response.content) > max_chars:
        failures.append(f"respuesta demasiado larga: {len(response.content)} > {max_chars}")

    return name, not failures, "; ".join(failures)


async def main() -> int:
    raw_pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    # soporta subconjuntos: `python evals/runner.py "06,21,22"` (gate de CI)
    patterns = [p.strip() for p in raw_pattern.split(",") if p.strip()] or [""]

    # Cargar y ACTIVAR plugins para escenarios que los requieran (los tools
    # solo se registran en el MCPHost al activar el plugin).
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    await plugin_manager.activate_all_configured()

    paths = sorted(p for p in SCENARIOS_DIR.glob("*.json") if any(pat in p.name for pat in patterns))
    if not paths:
        print(f"No hay escenarios que matcheen '{raw_pattern}'")
        return 1

    print(f"Corriendo {len(paths)} escenarios (ref {_git_ref()})...\n")
    exit_code = 0
    spec_version = ""
    for path in paths:
        try:
            name, ok, detail = await run_scenario(path)
        except Exception as exc:
            name, ok, detail = path.stem, False, f"excepción: {exc}"
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {status}  {name}" + (f"  — {detail}" if detail else ""))
        if not ok:
            exit_code = 1
    print(f"\nGolden set: {len(paths)} escenarios · spec {spec_version or 'culto.json'} · ref {_git_ref()}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
