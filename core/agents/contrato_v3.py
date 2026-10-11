"""Validador y linter de `agentspec/3` y `kernelspec/1` (ADR-0113 §1, HU-AOS-12).

El mismo validador corre en el CLI (`agent-core agent lint`), en CI y en la API al guardar un
borrador o publicar una versión: un AgentSpec o KernelSpec inválido no se guarda.

- `validar_spec(raw)`: la forma del contrato (pydantic), con un mensaje en español por error.
  Elige el contrato por el campo `contract` (o por `esperado`, el que exige la fila del agente).
- `lint_agentspec(spec, catalogo)`: lo que la forma no ve: acciones que el binario no trae,
  declarativas que chocan con una acción del binario (quedarían sombreadas, ADR-0052),
  dominios que no son FQDN públicos (anti-SSRF), duplicados, referencias rotas y
  credenciales literales en headers, URL o body (`core.agents.credenciales`).
- `lint_kernelspec(spec)`: ids de reglas repetidos y franjas vacías.
- `revisar_spec(raw, catalogo)`: las dos cosas. Lista vacía = sin problemas.

Puro: no lee base, red ni entorno. El catálogo lo arma quien llama
(`core.kernel.procedures.catalogo_de_acciones`), igual que para `procedure/1`.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Literal

from agentcore_contracts import AgentSpecV3, KernelSpec
from pydantic import ValidationError

from core.agents.credenciales import credenciales_literales, referencias_de_tool
from core.kernel.procedures.catalogo import Catalogo
from core.kernel.procedures.linter import Issue, _mensaje, _ruta, con_tope
from core.mcp.types import public_fqdn

Contrato = Literal["agentspec/3", "kernelspec/1"]
CONTRATOS: dict[str, type[AgentSpecV3] | type[KernelSpec]] = {
    "agentspec/3": AgentSpecV3,
    "kernelspec/1": KernelSpec,
}
# Tope de un spec serializado (CLI y API). Holgado: el General más grande de los negocios
# actuales, con sus bindings y declarativas, ocupa unos 20 KiB.
MAX_SPEC_BYTES = 256 * 1024

# Segmentos de la ruta de pydantic que nombran la rama de una unión, no un campo.
_RAMAS = frozenset({"str", "int", "float", "bool", "SecretRef", "constrained-str"})


def _ruta_limpia(loc: tuple[int | str, ...]) -> str:
    return _ruta(tuple(p for p in loc if not (isinstance(p, str) and (p in _RAMAS or p.startswith("function-")))))


def _mensaje_v3(error: Any, contrato: str) -> str:
    if error["type"] == "value_error":
        # Los validadores del contrato escriben su mensaje en español.
        texto = str(error.get("msg") or "").removeprefix("Value error, ")
        return texto[:1].upper() + texto[1:] + ("" if texto.endswith(".") else ".")
    return _mensaje(error, contrato)


# Profundidad máxima del documento: los campos libres (`parameters`, `body`, `outputSchema`,
# `extensions`) no pueden anidar sin tope (RecursionError, respuestas enormes).
MAX_PROFUNDIDAD = 32


def _estructura(raw: Any) -> str | None:
    """Recorre el documento sin recursión: profundidad y números no finitos (NaN o
    Infinity, que `json.loads` acepta y JSONB rechaza)."""
    pila: list[tuple[Any, int]] = [(raw, 1)]
    while pila:
        valor, nivel = pila.pop()
        if nivel > MAX_PROFUNDIDAD:
            return f"El documento anida más de {MAX_PROFUNDIDAD} niveles."
        if isinstance(valor, float) and not math.isfinite(valor):
            return "El documento tiene números no finitos (NaN o Infinity)."
        if isinstance(valor, dict):
            pila.extend((v, nivel + 1) for v in valor.values())
        elif isinstance(valor, list):
            pila.extend((v, nivel + 1) for v in valor)
    return None


def validar_spec(
    raw: Any, esperado: Contrato | None = None
) -> tuple[AgentSpecV3 | KernelSpec | None, list[Issue]]:
    """Valida la forma. Nunca lanza: devuelve el modelo o los errores.

    Con `esperado`, el documento debe ser de ese contrato (la fila del agente lo fija por
    su `kind`); sin él, se elige por el campo `contract`."""
    if not isinstance(raw, dict):
        return None, [Issue("forma", "(raíz)", "El spec debe ser un objeto JSON.")]
    problema = _estructura(raw)
    if problema is not None:
        return None, [Issue("forma", "(raíz)", problema)]
    contrato = raw.get("contract", esperado)
    if esperado is not None and contrato != esperado:
        return None, [Issue("contrato", "contract", f"Se espera el contrato {esperado}.")]
    modelo = CONTRATOS.get(contrato) if isinstance(contrato, str) else None
    if modelo is None:
        return None, [Issue("contrato", "contract", "Se espera agentspec/3 o kernelspec/1.")]
    try:
        return modelo.model_validate(raw), []
    except ValidationError as exc:
        issues: list[Issue] = []
        vistos: set[tuple[str, str]] = set()
        for error in exc.errors(include_url=False, include_input=False):
            issue = Issue("forma", _ruta_limpia(tuple(error["loc"])), _mensaje_v3(error, contrato))
            if (issue.path, issue.message) not in vistos:
                vistos.add((issue.path, issue.message))
                issues.append(issue)
        return None, issues


# ------------------------------------------------------------------ linter
def _repetidos(nombres: list[str]) -> list[str]:
    return sorted(n for n, veces in Counter(nombres).items() if veces > 1)


def lint_agentspec(spec: AgentSpecV3, catalogo: Catalogo) -> list[Issue]:
    """Problemas del AgentSpec v3 contra el catálogo de acciones del binario."""
    issues: list[Issue] = []
    declarativas = [t.name for t in spec.declarativeTools]

    for nombre in _repetidos([b.tool for b in spec.tools]):
        issues.append(Issue("binding_repetido", "tools", f"La acción '{nombre}' tiene más de un binding."))
    for i, binding in enumerate(spec.tools):
        if binding.tool not in catalogo and binding.tool not in declarativas:
            issues.append(
                Issue(
                    "accion_inexistente",
                    f"tools[{i}].tool",
                    f"La acción '{binding.tool}' no existe en el binario ni entre las tools declarativas.",
                )
            )

    for nombre in _repetidos(declarativas):
        issues.append(Issue("declarativa_repetida", "declarativeTools", f"La tool declarativa '{nombre}' está repetida."))
    for i, tool in enumerate(spec.declarativeTools):
        if tool.name in catalogo:
            issues.append(
                Issue(
                    "declarativa_sombreada",
                    f"declarativeTools[{i}].name",
                    f"'{tool.name}' es una acción del binario: la declarativa nunca correría. Usa otro nombre.",
                )
            )
        # Credenciales literales fuera de la lista del contrato (decisión del dueño 2026-10-11,
        # adenda de HU-AOS-13 en ADR-0113): headers, URL y body, siempre por referencia.
        crudo = tool.model_dump(mode="json", by_alias=True)
        for ruta in credenciales_literales(crudo):
            issues.append(
                Issue(
                    "credencial_literal",
                    f"declarativeTools[{i}].{ruta}",
                    "Las credenciales no admiten valores literales: en un header o el body usa "
                    '{"secret": "<nombre>"} y en la query de la URL {secret:<nombre>}; el userinfo '
                    "(usuario:clave@) va como header Authorization por referencia.",
                )
            )
        if tool.url.startswith("http://") and referencias_de_tool(crudo):
            issues.append(
                Issue(
                    "secreto_sin_https",
                    f"declarativeTools[{i}].url",
                    "Una tool con secretos solo llama por https://: la credencial no viaja en claro.",
                )
            )
        for j, dominio in enumerate(tool.allowedDomains):
            if public_fqdn(dominio) is None:
                issues.append(
                    Issue(
                        "dominio_no_publico",
                        f"declarativeTools[{i}].allowedDomains[{j}]",
                        "Solo dominios públicos con punto: ni IPs ni nombres internos.",
                    )
                )

    for i, regla in enumerate(spec.guardrails):
        if regla.kind == "required_tool" and regla.tool not in catalogo and regla.tool not in declarativas:
            issues.append(
                Issue(
                    "accion_inexistente",
                    f"guardrails[{i}].tool",
                    f"La acción '{regla.tool}' no existe en el binario ni entre las tools declarativas.",
                )
            )

    ids = [p.id for p in spec.procedures]
    for nombre in _repetidos(ids):
        issues.append(Issue("procedimiento_repetido", "procedures", f"El procedimiento '{nombre}' está repetido."))
    for i, expuesto in enumerate(spec.exposes.procedures):
        if expuesto not in ids:
            issues.append(
                Issue(
                    "expuesto_sin_declarar",
                    f"exposes.procedures[{i}]",
                    f"'{expuesto}' no está en `procedures`: el agente no puede exponer lo que no ejecuta.",
                )
            )
    for nombre in _repetidos([c.id for c in spec.exposes.consultas]):
        issues.append(Issue("consulta_repetida", "exposes.consultas", f"La consulta '{nombre}' está repetida."))
    return issues


def lint_kernelspec(spec: KernelSpec) -> list[Issue]:
    """Problemas del KernelSpec que la forma no ve."""
    issues: list[Issue] = []
    for nombre in _repetidos([r.id for r in spec.routing.rules]):
        issues.append(Issue("regla_repetida", "routing.rules", f"La regla '{nombre}' está repetida."))
    for i, regla in enumerate(spec.routing.rules):
        franja = regla.when.withinHours
        if franja is not None and franja.from_ == franja.to:
            issues.append(
                Issue("franja_vacia", f"routing.rules[{i}].when.withinHours", "La franja empieza y termina a la misma hora.")
            )
    return issues


def revisar_spec(raw: Any, catalogo: Catalogo, esperado: Contrato | None = None) -> list[Issue]:
    """Valida la forma y, si es válida, aplica el linter. Lista vacía = sin problemas.
    Devuelve a lo sumo `MAX_ISSUES` problemas más uno que cuenta el resto."""
    spec, issues = validar_spec(raw, esperado)
    if spec is None:
        return con_tope(issues)
    if isinstance(spec, AgentSpecV3):
        return con_tope(lint_agentspec(spec, catalogo))
    return con_tope(lint_kernelspec(spec))


__all__ = [
    "CONTRATOS",
    "MAX_PROFUNDIDAD",
    "MAX_SPEC_BYTES",
    "Contrato",
    "lint_agentspec",
    "lint_kernelspec",
    "revisar_spec",
    "validar_spec",
]
