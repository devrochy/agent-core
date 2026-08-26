"""Mensajes de respuesta del asistente desde el playbook `respuestas` (Sprint 2).

Los mensajes que el cliente ve (cierre de presupuesto, tool caída, fallback) son
texto del agente, no código: viven en `core/playbooks/respuestas.md` bajo
secciones `## <clave>`. Este módulo solo las extrae.
"""

from __future__ import annotations

from agentcore_contracts import AgentSpec


async def load_response(spec: AgentSpec, key: str) -> str | None:
    """Devuelve el texto de la sección `## <key>` del playbook respuestas."""
    from core.agents.playbooks import load_playbook

    content = await load_playbook(spec, "respuestas")
    if content is None:
        return None
    content = content.strip()
    if content.startswith("## "):
        content = content[3:]
    for section in content.split("\n## "):
        header, _, body = section.partition("\n")
        if header.strip() == key:
            return body.strip()
    return None