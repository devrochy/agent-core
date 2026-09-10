"""Runner de golden scenarios (assertions determinísticas, sin LLM-as-judge).

Uso:
  python evals/runner.py                       # golden set genérico del framework
  python evals/runner.py 03                    # solo los que matcheen "03"
  python evals/runner.py --tenant <slug>       # golden set del tenant (TENANTS_ROOT/<slug>/evals/)

Requiere LLM_API_KEY configurada (hace llamadas reales al proveedor activo).
Criterio (heredado de agent-sale ADR-012): assertions duras bloquean; el tono
se revisa manualmente. Correr antes de cada deploy, no en cada commit.

El spec y los escenarios de cada tenant viven en TENANTS_ROOT/<slug>/evals/
(dato por despliegue, gitignored) — el framework no mezcla datos de clientes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from agentcore_contracts import AgentSpec

from core.onboarding import tenants_root

SCENARIOS_DIR = Path(__file__).parent / "golden_scenarios"
SPECS_DIR = Path(__file__).parent / "specs"


def _load_spec(tenant: str | None) -> AgentSpec:
    """Spec real: del tenant (TENANTS_ROOT/<slug>/evals/spec.json) o el neutro
    del framework. ADR-0016: el gate evalúa la configuración real, no una sintética."""
    if tenant:
        path = tenants_root() / tenant / "evals" / "spec.json"
        if not path.exists():
            raise FileNotFoundError(f"Spec del tenant no encontrada: {path}")
    else:
        path = SPECS_DIR / "framework.json"
        if not path.exists():
            raise FileNotFoundError(f"Spec del framework no encontrada: {path}")
    return AgentSpec.model_validate(json.loads(path.read_text()))


def _scenario_paths(tenant: str | None, patterns: list[str]) -> list[Path]:
    base = tenants_root() / tenant / "evals" / "scenarios" if tenant else SCENARIOS_DIR
    return sorted(p for p in base.glob("*.json") if any(pat in p.name for pat in patterns))


def _git_ref() -> str:
    try:
        import subprocess

        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


TENANT1 = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def _tenant_id(slug: str) -> uuid.UUID:
    """Id del tenant por slug (los evals corren con un tenant REAL, nunca un UUID sintético)."""
    from sqlalchemy import select

    from core.db.models import Tenant
    from core.db.session import get_session

    async with get_session() as session:
        row = (await session.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if row is None:
        raise FileNotFoundError(f"Tenant no existe: {slug}")
    return row.id


async def _ensure_plugin_active(plugin_id: str, tenant_id: uuid.UUID) -> bool:
    """Activa un plugin para evals con su config default (si no está activo).

    Corre dentro de tenant_scope: save_config/activate persisten config por
    tenant (RLS + NOT NULL) y sin scope fallarían.
    """
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    module = plugin_manager.get(plugin_id)
    if module is None:
        return False
    async with tenant_scope(tenant_id):
        if not plugin_manager.is_active(plugin_id):
            await plugin_manager.save_config(plugin_id, {})
            await plugin_manager.activate(plugin_id)
        return plugin_manager.is_active(plugin_id)


async def run_scenario(path: Path, spec: AgentSpec, tenant_id: uuid.UUID) -> tuple[str, bool, str]:
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
        if not await _ensure_plugin_active(plugin_id, tenant_id):
            return name, True, f"SKIP: requiere {plugin_id} (no instalado)"

    provider = resolve_provider()
    ctx = PromptContext(
        assistant_name="Asistente de Prueba",
        business_description="Negocio de ejemplo para evaluación.",
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
    parser = argparse.ArgumentParser(description="Runner de golden scenarios")
    parser.add_argument("pattern", nargs="?", default="", help="Subconjunto por nombre (ej: 06)")
    parser.add_argument(
        "--tenant",
        default=None,
        help="Slug del tenant: corre su golden set desde TENANTS_ROOT/<slug>/evals/",
    )
    args = parser.parse_args()

    patterns = [p.strip() for p in args.pattern.split(",") if p.strip()] or [""]

    # Cargar y ACTIVAR plugins para escenarios que los requieran (los tools
    # solo se registran en el MCPHost al activar el plugin).
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()

    spec = _load_spec(args.tenant)
    paths = _scenario_paths(args.tenant, patterns)
    if not paths:
        base = "tenants/" + args.tenant + "/evals/scenarios" if args.tenant else "evals/golden_scenarios"
        print(f"No hay escenarios que matcheen '{args.pattern}' en {base}")
        return 1

    label = f"tenant {args.tenant}" if args.tenant else "framework"
    tenant_id = await _tenant_id(args.tenant) if args.tenant else TENANT1
    print(f"Corriendo {len(paths)} escenarios ({label}, ref {_git_ref()})...\n")
    exit_code = 0
    for path in paths:
        try:
            name, ok, detail = await run_scenario(path, spec, tenant_id)
        except Exception as exc:
            name, ok, detail = path.stem, False, f"excepción: {exc}"
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {status}  {name}" + (f"  — {detail}" if detail else ""))
        if not ok:
            exit_code = 1
    print(f"\nGolden set: {len(paths)} escenarios · spec {label} · ref {_git_ref()}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))