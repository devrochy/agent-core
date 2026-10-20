"""Tareas del worker del pack de conocimiento (ADR-0145 §2).

Registradas con `core.tasks.decorators.register_task`: `pack_knowledge.extraer` y
`pack_knowledge.embeber`. La lógica vive en `ingesta.py`; acá solo se nombran.
"""

from __future__ import annotations

from core.tasks.decorators import register_task
from pack_knowledge import ingesta

PLUGIN_ID = "pack_knowledge"


@register_task(PLUGIN_ID)
async def extraer(*, tenant_id: str, document_id: str) -> None:
    await ingesta.tarea_extraer(tenant_id=tenant_id, document_id=document_id)


@register_task(PLUGIN_ID)
async def embeber(*, tenant_id: str, document_id: str, sha256: str) -> None:
    await ingesta.tarea_embeber(tenant_id=tenant_id, document_id=document_id, sha256=sha256)
