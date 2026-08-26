"""SystemPromptBuilder: ENSAMBLADOR de bloques del system prompt (Sprint 2, ADR-0014).

Este módulo NO contiene texto de instrucciones al modelo: todo el contenido vive en
playbooks (`.md`, allowlist de `AgentSpec.playbooks`) y en datos del tenant. Aquí solo
se ensambla:

  1. Los playbooks del tenant en el orden del índice `AgentSpec.playbooks`,
     con sustitución de placeholders ({assistant_name}, {business_description},
     {language}).
  2. Bloques dinámicos del turno (voz de marca, skills activas, perfil y memorias)
     que cambian por turno — van al final para no invalidar el cache de los bloques
     estables (docs/PROMPT-CACHING-GUIDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentcore_contracts import AgentSpec


@dataclass
class PromptContext:
    assistant_name: str
    business_description: str
    language: str = "es"
    tone: str = "cercano"
    brand_voice: str = ""
    active_skills_summary: str = ""   # descripción de las skills activas del turno
    recalled_memories: list[str] = field(default_factory=list)  # Capa 3 (RAG)
    entity_profile: str = ""          # Capa 2 (perfil del cliente)

    @property
    def template_vars(self) -> dict[str, str]:
        language_names = {"es": "español", "en": "inglés", "pt": "portugués"}
        return {
            "assistant_name": self.assistant_name,
            "business_description": self.business_description,
            "language": language_names.get(self.language, self.language),
        }


async def _render_playbook(
    spec: AgentSpec, ref_name: str, vars_: dict[str, str]
) -> str | None:
    """Carga el playbook y sustituye placeholders. None si no está en el índice."""
    from core.agents.playbooks import load_playbook

    content = await load_playbook(spec, ref_name)
    if content is None:
        return None
    try:
        return content.format(**vars_)
    except KeyError:
        # playbook con placeholder no provisto: se incluye tal cual
        return content


async def build_system_blocks(spec: AgentSpec, ctx: PromptContext) -> list[str]:
    """Ensambla los bloques del system prompt desde el spec + playbooks + turno."""
    blocks: list[str] = []

    # 1. Playbooks del tenant, en el orden del índice (identidad, tono, reglas...)
    for ref in spec.playbooks:
        rendered = await _render_playbook(spec, ref.name, ctx.template_vars)
        if rendered:
            blocks.append(rendered)

    # 2. Voz de marca (dato del tenant, estable por negocio)
    if ctx.brand_voice:
        blocks.append(f"## Voz de marca\n{ctx.brand_voice}")

    # 3. Contexto dinámico del turno (va al final; no invalida el cache estable)
    dynamic_parts: list[str] = []
    if ctx.active_skills_summary:
        dynamic_parts.append(f"## Capacidades activas en este turno\n{ctx.active_skills_summary}")
    if ctx.entity_profile:
        dynamic_parts.append(f"## Lo que sabes de este cliente\n{ctx.entity_profile}")
    if ctx.recalled_memories:
        recalled = "\n".join(f"- {m}" for m in ctx.recalled_memories)
        dynamic_parts.append(f"## Hechos relevantes de interacciones pasadas\n{recalled}")
    if dynamic_parts:
        blocks.append("\n\n".join(dynamic_parts))

    return blocks