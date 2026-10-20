"""Tool `buscar_conocimiento` (ADR-0145 §5, HU-AOS-49 parte 3).

- **Quién la ve:** se ofrece solo si hay un agente v3 en foco, si ese agente la declara en `tools`
  (fail-closed, ADR-0118 §3) y si tiene al menos un documento `listo`. El predicado lo comprueba
  aunque no haya motor de procedimientos, que es el único que filtra por agente. Así no se ofrece en
  el runtime legacy ni gasta tokens en llamadas vacías.
- **Argumentos:** el LLM solo pasa `consulta`. El agente, el modo, `topK` y el umbral los inyecta el
  core desde la release del turno (`ToolContext.agent_id` y `ToolContext.knowledge`).
- **Resultado:** cada fragmento con `documento`, `seccion`, `paginas` y `texto` como campos tipados,
  para citar la fuente. Las reglas de uso (dato, no instrucción; qué decir si no hay resultados) viven
  en el playbook `conocimiento`, no acá (invariante nº3).
- **Auditoría:** `audit_log` guarda solo ids y puntajes (`resumir`). Borrar un documento borra su
  texto.
"""

from __future__ import annotations

from typing import Any, ClassVar

from core.llm.embeddings import EmbeddingsError
from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.shared import release_vigente
from core.shared.logger import get_logger
from pack_knowledge import busqueda

log = get_logger(__name__)

TOOL = "buscar_conocimiento"


def _paginas(desde: int | None, hasta: int | None) -> str | None:
    if desde is None:
        return None
    return str(desde) if hasta in (None, desde) else f"{desde}-{hasta}"


def resumir(resultado: dict[str, Any]) -> dict[str, Any]:
    """Lo que queda en `audit_log`: ids y puntajes, nunca el texto."""
    return {
        "fragmentos": [
            {"id": f.get("id"), "document_id": f.get("document_id"), "puntaje": f.get("puntaje")}
            for f in resultado.get("fragmentos") or []
            if isinstance(f, dict)
        ],
        **({"error": resultado["error"]} if "error" in resultado else {}),
        "tokens_consulta": resultado.get("tokens_consulta", 0),
    }


class KnowledgeSkill(BaseSkill):
    name = "conocimiento"
    description = "Búsqueda en los documentos del negocio que el dueño le subió a este agente"
    keywords: ClassVar[list[str]] = []

    async def _disponible(self, _conectados: set[str]) -> bool:
        agente = release_vigente.agente_del_turno()
        if agente is None or TOOL not in agente.tools:
            return False
        return await busqueda.hay_documentos_listos(agente.agent_id)

    async def buscar_conocimiento(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if ctx.agent_id is None:
            return {"fragmentos": [], "error": "sin_agente", "tokens_consulta": 0}
        consulta = str(args.get("consulta") or "")
        config = busqueda.Config.del_agente(ctx.knowledge)
        try:
            resultado = await busqueda.buscar(ctx.agent_id, consulta, config)
        except EmbeddingsError as exc:
            log.warning("knowledge_busqueda_sin_proveedor", codigo=exc.codigo)
            return {"fragmentos": [], "error": "busqueda_no_disponible", "tokens_consulta": 0}
        return {
            "fragmentos": [
                {
                    "id": str(f.chunk_id),
                    "document_id": str(f.document_id),
                    "documento": f.documento,
                    "seccion": f.seccion,
                    "paginas": _paginas(f.pagina_desde, f.pagina_hasta),
                    "texto": f.texto,
                    "puntaje": round(f.puntaje, 4),
                }
                for f in resultado.fragmentos
            ],
            "sin_resultados": not resultado.fragmentos,
            "tokens_consulta": resultado.tokens_consulta,
        }

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name=TOOL,
                description=(
                    "Busca en los documentos del negocio (preguntas frecuentes, catálogos, políticas) "
                    "la información para responder una pregunta del cliente. Úsala ANTES de responder "
                    "sobre productos, precios, horarios, envíos, cambios o políticas que no estén en la "
                    "conversación. No la uses para datos del cliente ni para pedidos."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "consulta": {
                            "type": "string",
                            "description": "La pregunta del cliente reformulada con sus palabras clave.",
                            "maxLength": busqueda.MAX_CONSULTA,
                        }
                    },
                    "required": ["consulta"],
                },
                handler=self.buscar_conocimiento,
                connection_predicate=self._disponible,
                resumen_de_auditoria=resumir,
            )
        ]
