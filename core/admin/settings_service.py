"""Configuración avanzada del agente (memoria y horario).

Fuente única del panel SSR y la API (ADR-0076/0077). La memoria es un valor por defecto de los agentes del negocio y
vive en `app_settings.agent_defaults` (ADR-0206 §6, `core.agents.defaults_negocio`). Las herramientas del negocio
se retiraron con el AgentSpec v2 (ADR-0206, HU-AOS-91).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from core.agents import defaults_negocio
from core.agents.defaults_negocio import DefaultsDelNegocio

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