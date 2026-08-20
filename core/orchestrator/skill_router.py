"""Skill Router: decide qué skills entran al turno.

Evita inflar el prompt con tools irrelevantes (más barato y más preciso):
- Skill sin keywords -> siempre activa (skills "core" del negocio).
- Skill con keywords -> activa si el mensaje O el historial reciente las menciona.
- Si NINGUNA skill matchea, se activan todas (mejor dar de más que bloquear
  al agente — el error es hacia capacidad, no hacia silencio).
"""

from __future__ import annotations

from core.mcp.types import SkillInfo


def select_relevant_skills(
    message: str, recent_context: str, available: list[SkillInfo]
) -> list[str]:
    if not available:
        return []

    haystack = f"{message}\n{recent_context}".lower()
    always_on = [s.name for s in available if not s.keywords]
    matched = [
        s.name
        for s in available
        if s.keywords and any(k.lower() in haystack for k in s.keywords)
    ]

    selected = always_on + matched
    if not selected:
        return [s.name for s in available]
    return selected
