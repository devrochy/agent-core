"""KnowledgeModule: conocimiento por agente (ADR-0145, HU-AOS-49).

- Parte 1: el modelo (tablas con RLS forzada), los topes por plan y el servicio de embeddings del
  core.
- Parte 2: la ingesta. API para subir, listar, borrar y reprocesar documentos
  (`admin_api.py`), y las tareas del worker que extraen, trocean y embeben (`jobs.py`).

Todavía no registra tools: `buscar_conocimiento` llega en la parte 3. Hasta entonces los documentos
se procesan, pero ningún agente los usa al responder.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pack_knowledge.jobs  # noqa: F401  # registra las tareas del worker (register_task)
from core.mcp.types import BaseModule
from pack_knowledge.admin_api import router as admin_api_router

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

PLUGIN_ID = "pack_knowledge"


class KnowledgeModule(BaseModule):
    id = PLUGIN_ID
    name = "Conocimiento"
    version = "0.2.0"
    description = "Documentos del negocio por agente (preguntas frecuentes, catálogos, políticas) para responder con ellos"
    display_name = "Conocimiento"
    category = "Conocimiento"
    requires_config = False
    min_plan = "free"
    setup_guide = (
        "Sube PDF, DOCX, TXT o Markdown en la ficha del agente. El agente los usará para responder "
        "cuando llegue la búsqueda (siguiente entrega)."
    )

    def admin_api_routes(self) -> list[Any]:
        return [admin_api_router]

    def on_activate(self, host: MCPHost) -> None:
        # Sin tools todavía (ADR-0145 §8): `buscar_conocimiento` llega en la parte 3.
        return None
