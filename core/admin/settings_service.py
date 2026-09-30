"""Configuración avanzada del agente (memoria, herramientas y horario).

Fuente única del panel SSR y la API (ADR-0076/0077). Todo se persiste como una
versión nueva del `AgentSpec` (versionable, ADR-0050/0051/0052) reusando
`core.agents.spec.save_spec_if_changed`.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from core.agents.spec import enabled_packs, load_active_spec, save_spec_if_changed
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
def read_memory(spec) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    data = spec.model_dump(mode="json")
    memory = data.get("memory") or {}
    rag = memory.get("rag") or {}
    return {
        "short_term_turns": int(memory.get("shortTermTurns", 30) or 30),
        "rag_top_k": int(rag.get("topK", 0) or 0),
        "rag_min_score": float(rag.get("minScore", 0.6) or 0.0),
    }


async def save_memory(
    tenant_id: uuid.UUID | None,
    *,
    short_term_turns: int,
    rag_top_k: int,
    rag_min_score: float,
) -> str | None:
    spec = await load_active_spec()
    data = spec.model_dump(mode="json")
    data["memory"]["shortTermTurns"] = max(1, min(200, short_term_turns))
    if rag_top_k and rag_top_k > 0:
        data["memory"]["rag"] = {
            "topK": max(1, min(20, rag_top_k)),
            "minScore": max(0.0, min(1.0, rag_min_score)),
        }
    else:
        data["memory"]["rag"] = None
    await save_spec_if_changed(spec, data, tenant_id)
    return None


# ------------------------------------------------------------------ habilidades (tools)
def read_skills(spec) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Herramientas editables (de packs habilitados) con su binding actual."""
    data = spec.model_dump(mode="json")
    bindings = {b.get("tool"): b for b in (data.get("tools") or [])}
    enabled = enabled_packs(spec)
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
    spec = await load_active_spec()
    data = spec.model_dump(mode="json")
    enabled = enabled_packs(spec)
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
