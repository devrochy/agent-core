"""SystemPromptBuilder: ensambla el system prompt dinámicamente.

Estructura en bloques (list[str], cada uno cacheable de forma independiente):

  Bloque 1 — Base: identidad del asistente + reglas de operación (estable).
  Bloque 2 — Tono: variante fija (calido | formal | divertido) — compartible.
  Bloque 3 — Voz de marca: texto libre del negocio (estable por negocio).
  Bloque 4 — Contexto dinámico: skills activas + memoria recuperada (RAG).
              Este bloque SÍ cambia por turno — por eso va al final y separado,
              para no invalidar el cache de los bloques 1-3.

REGLA DE ORO: los bloques 1-3 NUNCA contienen fecha/hora ni valores variables
(docs/PROMPT-CACHING-GUIDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

TONE_BLOCKS: dict[str, str] = {
    "calido": (
        "## Tono\n"
        "Habla de forma cercana y amable, como un buen vendedor de barrio que conoce a sus "
        "clientes. Usa un lenguaje sencillo y cálido. Puedes usar emojis con moderación (máximo "
        "uno por mensaje). Trata al cliente de 'tú'."
    ),
    "formal": (
        "## Tono\n"
        "Mantén un tono profesional y respetuoso. Usa 'usted' para dirigirte al cliente. "
        "No uses emojis ni expresiones coloquiales. Sé claro, preciso y cortés."
    ),
    "divertido": (
        "## Tono\n"
        "Sé alegre y con buen humor, sin perder profesionalismo. Puedes usar emojis y "
        "expresiones amigables. Haz que la conversación sea ligera y agradable. Trata al "
        "cliente de 'tú'."
    ),
}

BASE_RULES = """\
## Reglas de operación
- Responde SIEMPRE en {language}.
- Usa las herramientas disponibles para consultar información real. NUNCA inventes datos: \
si no tienes la información, dilo y ofrece escalar a un asesor humano.
- No menciones que eres un modelo de lenguaje ni hables de tus herramientas internas.
- Si el cliente pide hablar con una persona, usa la herramienta de escalamiento.
- Separa ideas distintas con un salto de línea en blanco (el sistema divide tu respuesta \
en mensajes cortos).
- Mantente dentro del ámbito del negocio. Ante temas ajenos, redirige una vez; si insiste, escala.
"""


@dataclass
class PromptContext:
    assistant_name: str
    business_description: str
    language: str = "es"
    tone: str = "calido"
    brand_voice: str = ""
    active_skills_summary: str = ""   # descripción de las skills activas del turno
    recalled_memories: list[str] = field(default_factory=list)  # Capa 3 (RAG)
    entity_profile: str = ""          # Capa 2 (perfil del cliente)


def build_system_blocks(ctx: PromptContext) -> list[str]:
    language_names = {"es": "español", "en": "inglés", "pt": "portugués"}
    language = language_names.get(ctx.language, ctx.language)

    # Bloque 1 — Base (estable, cacheable)
    block_base = (
        f"Eres {ctx.assistant_name}, el asistente virtual de: {ctx.business_description}\n\n"
        + BASE_RULES.format(language=language)
    )

    # Bloque 2 — Tono (variante fija, cacheable y compartible)
    block_tone = TONE_BLOCKS.get(ctx.tone, TONE_BLOCKS["calido"])

    blocks = [block_base, block_tone]

    # Bloque 3 — Voz de marca (estable por negocio)
    if ctx.brand_voice:
        blocks.append(f"## Voz de marca\n{ctx.brand_voice}")

    # Bloque 4 — Contexto dinámico (cambia por turno; va al final)
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
