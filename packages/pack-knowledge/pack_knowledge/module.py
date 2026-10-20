"""KnowledgeModule: conocimiento por agente (ADR-0145, HU-AOS-49).

Parte 1: el modelo (tablas con RLS forzada), los topes por plan y el servicio de embeddings del
core. Todavía no registra tools ni rutas: la ingesta (subida, extracción y troceado) es la
parte 2 y `buscar_conocimiento`, la parte 3. Sin tools, la habilidad instalada no cambia lo que
hace ningún agente.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.mcp.types import BaseModule

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

PLUGIN_ID = "pack_knowledge"


class KnowledgeModule(BaseModule):
    id = PLUGIN_ID
    name = "Conocimiento"
    version = "0.1.0"
    description = "Documentos del negocio por agente (preguntas frecuentes, catálogos, políticas) para responder con ellos"
    display_name = "Conocimiento"
    category = "Conocimiento"
    requires_config = False
    min_plan = "free"
    setup_guide = "En preparación: la subida de documentos llega en la siguiente entrega."

    def on_activate(self, host: MCPHost) -> None:
        # Sin tools en la parte 1 (ADR-0145 §8): `buscar_conocimiento` llega en la parte 3.
        return None
