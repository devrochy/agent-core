"""El AgentSpec del turno desde la release (HU-AOS-15, adenda de ADR-0117).

Con `tenants.aos_runtime = 'v3'` el loop ejecuta la versión congelada del especialista en foco
(`agentspec/3`) y el KernelSpec del Coordinador (`kernelspec/1`) de la release de la
conversación, no el AgentSpec v2 de `agent_specs`. El loop lee hoy la forma v2 en ~25 puntos
(persona, modelo, memoria, playbooks, bindings, políticas, packs, guardrails y tools
declarativas), así que el punto de enganche es UNO: `spec_del_turno` devuelve un objeto del
contrato v2 `AgentSpec` armado desde el contenido congelado, la inversa exacta de la tabla
campo por campo del lector tolerante (ADR-0113 §5). Es un lector tolerante **inverso y
transitorio**: se retira cuando el loop lea v3 directo (HU-AOS-46).

Reglas:
- La vista nunca se persiste ni vuelve a `agent_specs`; `extensions` solo transporta lo que v2
  ya transportaba (`guardrails` y `tools` declarativas): nada nuevo del v3 viaja por ahí (lo
  nuevo, como `writeNamespaces`, va por `core.shared.release_vigente`).
- Sin release (legacy, sin conexión, sin release activa) devuelve el mismo v2: cero cambio.
- Fail-closed: un contenido congelado que no valida el contrato es `VistaInvalida` (sube por el
  camino de error del turno). Una tool declarativa con referencias a secretos en la URL o el
  body se omite (hoy `core/mcp/declarative.py` solo resuelve referencias en headers: incluirla
  mandaría el placeholder literal a un tercero); el comando `migrar-a-v3` la reporta y bloquea
  el negocio hasta la parte 2.

Importa solo contratos y `core.shared.release_vigente` (módulo hoja): nunca `core.releases`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from agentcore_contracts import AgentSpec, AgentSpecV3, KernelSpec
from pydantic import ValidationError

from core.agents.credenciales import referencias_en_body, referencias_en_url
from core.shared import release_vigente
from core.shared.logger import get_logger
from core.shared.release_vigente import AgenteCongelado

log = get_logger(__name__)


class VistaInvalida(RuntimeError):
    """El contenido congelado de la release no cabe en los contratos v3 (o falta el Coordinador)."""


def validar_componentes(agente: AgenteCongelado, kernel: AgenteCongelado | None) -> tuple[AgentSpecV3, KernelSpec]:
    if kernel is None:
        raise VistaInvalida("la release no trae el KernelSpec del Coordinador")
    try:
        v3 = AgentSpecV3.model_validate(dict(agente.content))
        ks = KernelSpec.model_validate(dict(kernel.content))
    except ValidationError as exc:
        rutas = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise VistaInvalida(f"la versión congelada no pasa el contrato: {', '.join(rutas)[:300]}") from None
    return v3, ks


def declarativas_omitidas(v3: AgentSpecV3) -> list[str]:
    """Nombres de las tools declarativas que la vista v2 NO incluye: las que llevan referencias
    a secretos en la URL o en el body (límite de la parte 1; ver el docstring del módulo)."""
    return sorted(
        t.name for t in v3.declarativeTools if referencias_en_url(t.url) or referencias_en_body(t.body)
    )


def _declarativas_v2(v3: AgentSpecV3, omitidas: set[str]) -> list[dict[str, Any]]:
    salida: list[dict[str, Any]] = []
    for t in v3.declarativeTools:
        if t.name in omitidas:
            continue
        salida.append(
            {
                "name": t.name,
                "description": t.description,
                "method": t.method,
                "url": t.url,
                "allowed_domains": list(t.allowedDomains),
                # Un `SecretRef` queda como `{"secret": nombre}`: `declarative.py` lo resuelve al ejecutar.
                "headers": {k: (v if isinstance(v, str) else {"secret": v.secret}) for k, v in t.headers.items()},
                "parameters": t.parameters,
                "body": t.body,
            }
        )
    return salida


def spec_v2_desde_release(
    agente: AgenteCongelado,
    kernel: AgenteCongelado | None,
    *,
    tenant_id: uuid.UUID,
    skills: tuple[str, ...] | list[str],
    tenant_plugins: tuple[str, ...] | list[str],
) -> AgentSpec:
    """La forma v2 que lee el loop, desde la versión congelada del especialista y el KernelSpec
    (la inversa de la tabla de ADR-0113 §5). Puro: no lee base ni disco."""
    v3, ks = validar_componentes(agente, kernel)
    omitidas = set(declarativas_omitidas(v3))
    if omitidas:
        log.warning("declarativa_omitida_refs_url_body", agent_key=agente.key, tools=sorted(omitidas))
    extensiones: dict[str, Any] = {k: v for k, v in (v3.extensions or {}).items() if k not in ("tools", "guardrails")}
    extensiones["guardrails"] = [g.model_dump(mode="json", exclude_none=True) for g in v3.guardrails]
    extensiones["tools"] = _declarativas_v2(v3, omitidas)
    rag = v3.memory.rag
    return AgentSpec.model_validate(
        {
            "id": agente.key,
            # Texto libre en el contrato: identifica la versión congelada en `usage_ledger`.
            "version": f"{agente.key}@v3:{agente.version}",
            "tenantId": str(tenant_id),
            "persona": {
                # La voz es de la conexión (KernelSpec, ADR-0113 §4); el tope y las frases, del agente.
                "role": ks.voice.role,
                "tone": ks.voice.tone,
                "warmth": ks.voice.warmth,
                "emojis": ks.voice.emojis,
                "maxLength": v3.persona.maxLength,
                "forbiddenPhrases": list(v3.persona.forbiddenPhrases),
            },
            "model": {
                "primary": v3.model.primary,
                "fallback": v3.model.fallback,
                "router": v3.model.router,
                "temperature": v3.model.temperature,
            },
            "memory": {
                "shortTermTurns": v3.memory.shortTermTurns,
                "rag": {"topK": rag.topK, "minScore": rag.minScore} if rag is not None else None,
            },
            "playbooks": [{"name": p.name, "description": p.description} for p in v3.playbooks],
            "tools": [
                {
                    "tool": b.tool,
                    "enabled": b.enabled,
                    "scopes": list(b.scopes),
                    "maxCallsPerConversation": b.maxCallsPerConversation,
                    "requiresConfirmation": b.requiresConfirmation,
                }
                for b in v3.tools
            ],
            "policies": {
                "maxToolCallsPerTurn": v3.policies.maxToolCallsPerTurn,
                "budgetUsdPerConversation": ks.policies.budgetUsdPerConversation,
            },
            # La habilitación congelada en el manifiesto (hasta HU-AOS-16, `pack_installations`).
            "packs": sorted({*skills, *tenant_plugins}),
            "extensions": extensiones,
        }
    )


def spec_del_turno(spec: AgentSpec) -> AgentSpec:
    """El AgentSpec que ejecuta el turno: la vista v2 de la release vigente (`aos_runtime = v3`)
    o `spec` tal cual sin release (legacy): el único punto de enganche del loop."""
    actual = release_vigente.vigente()
    if actual is None or actual.foco is None:
        return spec
    vista = spec_v2_desde_release(
        actual.foco,
        release_vigente.kernel_del_turno(),
        tenant_id=actual.tenant_id,
        skills=actual.skills,
        tenant_plugins=actual.tenant_plugins,
    )
    log.info("turn_spec_from_release", agent_key=actual.foco.key, agent_version=actual.foco.version)
    return vista


def vista_de(agente: AgenteCongelado, kernel: AgenteCongelado | None, tenant_id: uuid.UUID) -> Mapping[str, Any]:
    """Solo para reportes (CLI): la vista como dict, sin tocar la ranura del turno."""
    return spec_v2_desde_release(agente, kernel, tenant_id=tenant_id, skills=(), tenant_plugins=()).model_dump(
        mode="json"
    )


__all__ = [
    "VistaInvalida",
    "declarativas_omitidas",
    "spec_del_turno",
    "spec_v2_desde_release",
    "validar_componentes",
    "vista_de",
]
