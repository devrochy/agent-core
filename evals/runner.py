"""Runner de golden scenarios (assertions determinísticas, sin LLM-as-judge).

Uso:
  python evals/runner.py                       # golden set genérico del framework
  python evals/runner.py 03                    # solo los que matcheen "03"
  python evals/runner.py --tenant <slug>       # golden set del tenant (TENANTS_ROOT/<slug>/evals/)
  python evals/runner.py 06 --grabacion replay # desde la grabación, sin red ni LLM_API_KEY
  python evals/runner.py 06 --grabacion record # regraba el escenario (LLM real: OK del dueño)

Sin `--grabacion` requiere LLM_API_KEY (hace llamadas reales al proveedor activo). Las
grabaciones viven en `evals/cassettes/golden/` (ver `evals/README.md`).
Criterio (heredado de agent-sale ADR-012): assertions duras bloquean; el tono
se revisa manualmente. Correr antes de cada deploy, no en cada commit.

El spec y los escenarios de cada tenant viven en TENANTS_ROOT/<slug>/evals/
(dato por despliegue, gitignored) — el framework no mezcla datos de clientes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from agentcore_contracts import AgentSpec

from core.onboarding import tenants_root

if TYPE_CHECKING:
    from core.llm.types import LLMMessage

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


def _build_messages(conversation: list[dict]) -> list[LLMMessage]:
    """Arma el historial de un escenario con la MISMA forma que usa
    `core/orchestrator/loop.py` dentro de un turno real (líneas ~296-316):
    un tool_call declarado en el JSON del escenario se traduce a un
    `LLMMessage(role="assistant", tool_calls=[...])` seguido de un
    `LLMMessage(role="tool", tool_call_id=..., content=...)` por cada
    resultado.

    Sin esto (el comportamiento de antes), el modelo solo veía la
    AFIRMACIÓN en texto de un turno "assistant" anterior -- sin evidencia
    estructurada de que la tool realmente corrió. Causa confirmada de la
    flakiness de `venta-recomendar-producto-03`/`seguimiento-calificar-lead-22`
    (ver docs/ROADMAP.md): el modelo a veces no confiaba en que el dato ya
    estuviera resuelto y volvía a consultar inventario o repetía `crear_lead`.

    Un turno sin `tool_calls` declarados (el caso de TODOS los demás
    escenarios) produce exactamente el mismo `LLMMessage(role, content)`
    de antes -- cero cambio de comportamiento para el resto del golden set.
    """
    from core.llm.types import LLMMessage, LLMToolCall

    messages: list[LLMMessage] = []
    for i, m in enumerate(conversation):
        declared = m.get("tool_calls", [])
        messages.append(
            LLMMessage(
                role=m["role"],
                content=m.get("content", ""),
                tool_calls=[
                    LLMToolCall(id=f"eval_{i}_{j}", name=tc["name"], arguments=tc["arguments"])
                    for j, tc in enumerate(declared)
                ],
            )
        )
        for j, tc in enumerate(declared):
            messages.append(
                LLMMessage(
                    role="tool",
                    tool_call_id=f"eval_{i}_{j}",
                    content=json.dumps(tc.get("result", {}), ensure_ascii=False),
                )
            )
    return messages


async def _predicado_ok(tool, tenant_id: uuid.UUID) -> bool:  # type: ignore[no-untyped-def]
    """`connection_predicate` de la tool dentro del `tenant_scope` del negocio."""
    if tool.connection_predicate is None:
        return True
    from core.shared.tenant import tenant_scope

    try:
        async with tenant_scope(tenant_id):
            return bool(await tool.connection_predicate(set()))
    except Exception:
        return False


def _falla_tools_prohibidas(valor: object, llamadas: list[str]) -> str:
    """Evalúa `must_not_call_tools`; devuelve el motivo de la falla o "".

    - `true`: no debe llamar ninguna tool (los escenarios del framework).
    - lista de nombres: solo esas cuentan; las demás tools del turno no fallan.
    - `false`, lista vacía o ausente: sin restricción.
    Otro tipo (un string suelto, un número) es un error del escenario y falla.
    """
    if valor is True:
        return f"llamó tools inesperadas: {llamadas}" if llamadas else ""
    if valor is False:
        return ""
    if not isinstance(valor, list) or not all(isinstance(n, str) for n in valor):
        return f"must_not_call_tools inválido (true/false o lista de nombres): {valor!r}"
    vetadas = set(valor)
    prohibidas = [n for n in llamadas if n in vetadas]
    return f"llamó tools prohibidas: {prohibidas} (llamó: {llamadas})" if prohibidas else ""


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
    from core.llm import cassette, resolve_provider
    from core.llm.prompt_builder import PromptContext, build_system_blocks
    from core.mcp.host import mcp_host
    from core.mcp.translator import to_llm_schemas

    settings = get_settings()
    if not settings.llm_api_key and cassette.modo_activo() != "replay":
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
    # `now` fijo opcional (ISO 8601 con zona): escenarios que dependen de fechas
    # relativas ("el próximo miércoles") se evalúan de forma determinística.
    if scenario.get("now"):
        from datetime import datetime

        ctx.now = datetime.fromisoformat(scenario["now"])
    # Fidelidad con producción (ADR-0054/#155): el eval debe ver el MISMO subconjunto
    # de tools que el asistente real — packs habilitados por el spec + flujo resuelto.
    # Antes se pasaban TODAS las skills del host, así que un escenario podía pasar
    # aunque el flujo del tenant excluyera la tool.
    from core.agents.flow import render_flow, resolve_flow
    from core.agents.spec import enabled_packs

    enabled = enabled_packs(spec)
    candidate = [
        tool
        for tool in mcp_host.get_skill_tools([s.name for s in mcp_host.list_skills()])
        if (tool.plugin_id or "core") in enabled
    ]
    flow = await resolve_flow(spec, available={t.name for t in candidate})
    if flow is not None:
        by_name = {t.name: t for t in candidate}
        active = [by_name[name] for name in flow.tool_names if name in by_name]
        ctx.flow_summary = render_flow(flow)  # mismo bloque que el loop real (ADR-0054)
        # Igual que el loop (ADR-0109): las tools del core se ofrecen aunque el flujo
        # no las nombre, si su predicado lo permite (las `sub_*` solo con proveedor).
        en_flujo = {t.name for t in active}
        active += [
            t
            for t in candidate
            if t.always_available and t.name not in en_flujo and await _predicado_ok(t, tenant_id)
        ]
    else:
        active = candidate
    tools = to_llm_schemas(active)
    messages = _build_messages(scenario["conversation"])
    response = await provider.generate(
        system_blocks=await build_system_blocks(spec, ctx), messages=messages, tools=tools or None
    )

    failures: list[str] = []
    text_lower = response.content.lower()

    for banned in assertions.get("must_not_contain", []):
        if banned.lower() in text_lower:
            failures.append(f"contiene texto prohibido: '{banned}'")

    falla = _falla_tools_prohibidas(
        assertions.get("must_not_call_tools", False), [t.name for t in response.tool_calls]
    )
    if falla:
        failures.append(falla)

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

    if "must_call_any" in assertions:
        called = [t.name for t in response.tool_calls]
        allowed = assertions["must_call_any"]
        if not (set(allowed) & set(called)):
            failures.append(f"no llamó ninguna de {allowed} (llamó: {called})")

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
    parser.add_argument("--k", type=int, default=1, help="Intentos por escenario (pass^k, HU-ARCH-28)")
    parser.add_argument(
        "--umbral",
        type=float,
        default=1.0,
        help="pass@1 mínimo (0..1) para salir con 0; con 1.0 cualquier falla rompe el gate",
    )
    parser.add_argument("--reporte", default="", help="Ruta del reporte JSON (opcional)")
    sys.path.insert(0, str(Path(__file__).parent))
    import grabaciones

    grabaciones.agregar_argumentos(parser)
    from core.shared.logger import configure_console_logging

    configure_console_logging()  # tracebacks sin variables locales: nunca imprimen la config
    args = parser.parse_args()
    from core.llm import cassette

    try:
        grabaciones.activar(args.grabacion)
    except cassette.ErrorGrabacion as exc:
        print(f"\n{exc}")
        return grabaciones.SALIDA_GRABACION
    if args.tenant and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.tenant):
        print(f"Slug inválido: {args.tenant!r}")
        return 1
    raiz_grabaciones = grabaciones.raiz(
        args.grabaciones, tenants_root() / args.tenant / "evals" if args.tenant else None
    ) / "golden"
    from consistencia import (
        Intento,
        ResultadoEscenario,
        escribir_reporte,
        linea,
        reporte,
        resumir,
        texto_resumen,
    )

    patterns = [p.strip() for p in args.pattern.split(",") if p.strip()] or [""]

    # Cargar y ACTIVAR plugins para escenarios que los requieran (los tools
    # solo se registran en el MCPHost al activar el plugin).
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()
    # Tools del core (escalamiento, memoria, `sub_*`…): el loop real las tiene porque
    # `core/main.py` las registra al importar; el runner no importa `core.main`.
    from core.mcp.host import mcp_host as _host
    from core.tools.core_skill import register_core_skills

    register_core_skills(_host)

    spec = _load_spec(args.tenant)
    paths = _scenario_paths(args.tenant, patterns)
    if not paths:
        base = "tenants/" + args.tenant + "/evals/scenarios" if args.tenant else "evals/golden_scenarios"
        print(f"No hay escenarios que matcheen '{args.pattern}' en {base}")
        return 1

    label = f"tenant {args.tenant}" if args.tenant else "framework"
    tenant_id = await _tenant_id(args.tenant) if args.tenant else TENANT1
    k = max(1, args.k)
    print(f"Corriendo {len(paths)} escenarios con k={k} ({label}, ref {_git_ref()})...\n")
    results: list[ResultadoEscenario] = []
    extra = f"--tenant {args.tenant}" if args.tenant else ""
    try:
        for path in paths:
            escenario = ResultadoEscenario(nombre=path.stem)
            regrabar = grabaciones.comando_regrabar("evals/runner.py", path.stem, f"{extra} --k {k}".strip())
            with cassette.escena(raiz_grabaciones / f"{path.stem}.json", regrabar=regrabar):
                for _ in range(k):
                    inicio = time.monotonic()
                    try:
                        name, ok, detail = await run_scenario(path, spec, tenant_id)
                    except cassette.ErrorGrabacion:
                        raise  # la grabación falta o cambió: se corta la corrida, no es un fallo del modelo
                    except Exception as exc:
                        name, ok, detail = path.stem, False, f"excepción: {exc}"
                    escenario.nombre = name
                    escenario.intentos.append(Intento(ok=ok, detalle=detail, segundos=round(time.monotonic() - inicio, 1)))
            print(linea(escenario))
            results.append(escenario)
    except cassette.ErrorGrabacion as exc:
        print(f"\n{exc}")
        return grabaciones.SALIDA_GRABACION
    resumen = resumir(results, k)
    print(f"\n{texto_resumen('Golden set', resumen, args.umbral)} · spec {label} · ref {_git_ref()}")

    # Persistir la corrida atada a la versión del AgentSpec (ADR-0051). Solo con
    # tenant real (el framework no escribe en la BD de un cliente).
    # En replay no se mide la consistencia del modelo (se repite lo grabado): no se guarda.
    if args.tenant and cassette.modo_activo() != "replay":
        await _persist_run(tenant_id, spec.version, results, k)
    if args.reporte:
        escribir_reporte(args.reporte, reporte(f"golden:{label}", results, k))
    # El gate se mide sobre pass@1 (ADR-0119): con k = 1 y umbral 1.0 es el de siempre.
    return 0 if resumen.pass_1 >= args.umbral else 1


async def _persist_run(tenant_id: uuid.UUID, spec_version: str | None, results: list, k: int = 1) -> None:  # type: ignore[type-arg]
    """Guarda `eval_runs` + `eval_results` del tenant (best-effort, no rompe el gate)."""
    try:
        from consistencia import persistir

        await persistir(tenant_id, spec_version, _git_ref(), results, k)
        passed = sum(r.ok for r in results)
        print(f"  (corrida guardada: {passed}/{len(results)}, k={k}, spec v{spec_version})")
    except Exception as exc:
        print(f"  (no se pudo persistir la corrida: {exc})")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))