"""La toma humana queda en la tarea (HU-AOS-28, ADR-0169 §5).

Cuando una persona toma una conversación escalada se escribe `taken` (actor `human`) en cada tarea
abierta, y `released` al devolverla al agente o resolverla. Son anotaciones: no cambian la proyección
(`state.aplicar`), y la regla de lo terminal no se relaja: las tareas cerradas no se anotan.

Quien llama ya bloqueó la conversación (orden de bloqueos conversación → tareas, `state.registrar`).
Una tarea que se cerró entre la lectura y la escritura se salta sin tumbar la toma.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.db.models import Conversation
from core.kernel import state
from core.shared.logger import get_logger

log = get_logger(__name__)


async def bloquear_conversacion(session: Any, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation | None:
    """La conversación con `FOR UPDATE` (primer bloqueo de la toma)."""
    return (
        await session.execute(
            select(Conversation)
            .where(Conversation.tenant_id == tenant_id, Conversation.id == conversation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def anotar(
    session: Any,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    tipo: str,
    *,
    admin_id: uuid.UUID,
    escalation_id: uuid.UUID | None = None,
) -> int:
    """Escribe `tipo` (`taken` o `released`) en cada tarea abierta. Devuelve cuántas anotó."""
    if tipo not in state.ANOTACIONES_HUMANAS:
        raise ValueError(f"tipo de anotación desconocido: {tipo}")
    data = {"admin_id": str(admin_id), **({"escalation_id": str(escalation_id)} if escalation_id else {})}
    anotadas = 0
    for tarea in await state.tareas_abiertas(session, tenant_id=tenant_id, conversation_id=conversation_id):
        try:
            async with session.begin_nested():
                await state.registrar(
                    session, tenant_id=tenant_id, conversation_id=conversation_id, task_id=tarea.id,
                    tipo=tipo, data=data, actor="human",
                )
            anotadas += 1
        except state.TransicionInvalida:
            log.info("toma_humana_tarea_saltada", task_id=str(tarea.id), tipo=tipo)
    return anotadas


__all__ = ["anotar", "bloquear_conversacion"]
