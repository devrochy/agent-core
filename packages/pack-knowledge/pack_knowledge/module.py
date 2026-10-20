"""KnowledgeModule: conocimiento por agente (ADR-0145, HU-AOS-49).

- Parte 1: el modelo (tablas con RLS forzada), los topes por plan y el servicio de embeddings del
  core.
- Parte 2: la ingesta. API para subir, listar, borrar y reprocesar documentos
  (`admin_api.py`), y las tareas del worker que extraen, trocean y embeben (`jobs.py`).
- Parte 3: la búsqueda híbrida (`busqueda.py`), la tool `buscar_conocimiento` (`skills.py`) y el
  playbook `conocimiento`. Un agente la usa solo si la declara en `tools` y tiene documentos listos.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import pack_knowledge.jobs  # noqa: F401  # registra las tareas del worker (register_task)
from core.mcp.types import BaseModule
from pack_knowledge.admin_api import router as admin_api_router
from pack_knowledge.skills import KnowledgeSkill

if TYPE_CHECKING:
    from core.mcp.host import MCPHost

PLUGIN_ID = "pack_knowledge"


class KnowledgeModule(BaseModule):
    id = PLUGIN_ID
    name = "Conocimiento"
    version = "0.3.0"
    description = "Documentos del negocio por agente (preguntas frecuentes, catálogos, políticas) para responder con ellos"
    display_name = "Conocimiento"
    category = "Conocimiento"
    requires_config = False
    min_plan = "free"
    setup_guide = (
        "Sube PDF, DOCX, TXT o Markdown en la ficha del agente y agrega a sus herramientas "
        "`buscar_conocimiento` y a sus playbooks `conocimiento`: responderá con esos documentos."
    )
    declared_skills: ClassVar[list[type]] = [KnowledgeSkill]

    def admin_api_routes(self) -> list[Any]:
        return [admin_api_router]

    def on_activate(self, host: MCPHost) -> None:
        host.register_skill(KnowledgeSkill())
