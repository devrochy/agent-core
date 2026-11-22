"""Configuración avanzada del agente (memoria, herramientas y horario).

Fuente única del panel SSR y la API (ADR-0076/0077). Las herramientas se persisten como una
versión nueva del `AgentSpec` (versionable, ADR-0050/0051/0052) reusando
`core.agents.spec.save_spec_if_changed`; la memoria es un valor por defecto de los agentes del
negocio y vive en `app_settings.agent_defaults` (ADR-0206 §6, `core.agents.defaults_negocio`).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from core.agents import defaults_negocio
from core.agents.defaults_negocio import DefaultsDelNegocio
from core.agents.spec import load_active_spec, save_spec_if_changed
from core.mcp.host import mcp_host

# ------------------------------------------------------------------ horario
_RANGE_RE = re.compile(r"^([0-6])\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})$")


def format_business_ranges(escalation_config: dict | None) -> str:
    """Franjas de horario a texto editable ("día HH:MM-HH:MM" por línea)."""
    ranges = ((escalation_config or {}).get("business_hours") or {}).get("ranges") or []
    lines = []
    for r in ranges:
        day, start, end = r.get("day"), r.get("from"), r.get("to")
        if day is not None and start and end:
            lines.append(f"{day} {start}-{end}")
    return "\n".join(lines)


def parse_business_ranges(text: str) -> list[dict[str, Any]]:
    """Parsea "día HH:MM-HH:MM" por línea a la forma del contrato. Ignora basura."""
    ranges: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        m = _RANGE_RE.fullmatch(line.strip())
        if m:
            ranges.append({"day": int(m.group(1)), "from": m.group(2), "to": m.group(3)})
    return ranges


def validate_business_ranges(ranges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normaliza franjas estructuradas (day 0-6, HH:MM). Descarta inválidas."""
    out: list[dict[str, Any]] = []
    for r in ranges or []:
        try:
            day = int(r.get("day"))
            start = str(r.get("from") or "")
            end = str(r.get("to") or "")
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6 and _RANGE_RE.fullmatch(f"{day} {start}-{end}"):
            out.append({"day": day, "from": start, "to": end})
    return out


# ------------------------------------------------------------------ memoria
def read_memory(defaults: DefaultsDelNegocio) -> dict[str, Any]:
    """Memoria de los valores por defecto del negocio. Sin RAG: top-k 0 y la similitud por defecto."""
    rag = defaults.memory.rag
    return {
        "short_term_turns": defaults.memory.shortTermTurns,
        "rag_top_k": rag.topK if rag is not None else 0,
        "rag_min_score": float(rag.minScore) if rag is not None else 0.6,
    }


async def save_memory(
    tenant_id: uuid.UUID | None,
    *,
    short_term_turns: int,
    rag_top_k: int,
    rag_min_score: float,
) -> DefaultsDelNegocio | None:
    """Guarda la memoria en `agent_defaults` (merge: el resto de los valores no cambia). Sin negocio no hace nada."""
    if tenant_id is None:
        return None
    rag = (
        {"topK": max(1, min(20, rag_top_k)), "minScore": max(0.0, min(1.0, rag_min_score))}
        if rag_top_k and rag_top_k > 0
        else None
    )
    return await defaults_negocio.guardar(
        tenant_id, {"memory": {"shortTermTurns": max(1, min(200, short_term_turns)), "rag": rag}}
    )


# ------------------------------------------------------------------ habilidades (tools)
def read_skills(spec, enabled: set[str]) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Herramientas editables (de los packs del negocio, `packs_del_negocio`) con su binding actual."""
    data = spec.model_dump(mode="json")
    bindings = {b.get("tool"): b for b in (data.get("tools") or [])}
    items: list[dict[str, Any]] = []
    for tool in mcp_host.get_tools():
        if tool.plugin_id not in enabled:
            continue
        binding = bindings.get(tool.name) or {}
        items.append(
            {
                "name": tool.name,
                "plugin_id": tool.plugin_id,
                "description": tool.description,
                "enabled": bool(binding.get("enabled", True)),
                "max_calls_per_conversation": binding.get("maxCallsPerConversation"),
                "requires_confirmation": bool(binding.get("requiresConfirmation", False)),
            }
        )
    items.sort(key=lambda t: (t["plugin_id"], t["name"]))
    return items


async def save_skills(
    tenant_id: uuid.UUID | None,
    *,
    tools: list[dict[str, Any]],
) -> str | None:
    """Aplica on/off y límites SOLO a las tools de packs habilitados."""
    from core.agents.instalaciones_agente import packs_del_negocio

    spec = await load_active_spec()
    data = spec.model_dump(mode="json")
    enabled = await packs_del_negocio()
    editable = {t.name for t in mcp_host.get_tools() if t.plugin_id in enabled}

    bindings_by_tool = {b["tool"]: dict(b) for b in data.get("tools", [])}
    for item in tools:
        name = str(item.get("name") or "")
        if name not in editable:
            continue
        try:
            max_calls = int(item.get("max_calls_per_conversation") or 0)
        except (TypeError, ValueError):
            max_calls = 0
        binding = bindings_by_tool.get(name, {"tool": name, "scopes": []})
        binding["enabled"] = bool(item.get("enabled", True))
        binding["maxCallsPerConversation"] = max_calls if max_calls > 0 else None
        binding["requiresConfirmation"] = bool(item.get("requires_confirmation", False))
        bindings_by_tool[name] = binding
    data["tools"] = list(bindings_by_tool.values())
    await save_spec_if_changed(spec, data, tenant_id)
    return None
