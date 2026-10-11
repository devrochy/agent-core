"""Tareas de una conversación (ADR-0114, HU-AOS-04), solo lectura.

- `GET /api/v1/admin/conversations/{id}/tasks` — tareas en foco, en pausa y cerradas (las 50
  más recientes), con sus datos, su etapa y lo que espera confirmación.
- `GET /api/v1/admin/conversations/{id}/tasks/{task_id}/events` — historia de la tarea
  (`task_events`) por `seq`, paginada.

Es lo que verá el panel "En curso" (HU-AOS-28). Sección `conversations` (la misma que deja ver
los mensajes, que ya traen esos datos). 404 si la conversación o la tarea no son del negocio
activo; como el resto de la API de conversaciones, las del simulador (`TEST_CHANNEL`) no se
exponen. Un GET nunca escribe: no abre episodios ni expira tareas.

Los datos `payment` (referencias de cobro) se devuelven enmascarados, en la tarea y en los eventos.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.pagination import Page, PageParams, build_page, page_params
from core.db.models import Conversation, ConversationTask, TaskEvent
from core.db.session import get_session
from core.shared.test_mode import TEST_CHANNEL

router = APIRouter(prefix="/admin/conversations", tags=["admin · conversaciones"])

MAX_TAREAS = 50
OCULTO = "•••"


def _visible(valor: Any, data_class: Any) -> Any:
    return OCULTO if data_class == "payment" else valor


class TaskSlot(BaseModel):
    value: Any
    source: str | None = None
    actor: str | None = None
    confirmed: bool = False
    data_class: str | None = None


class TaskItem(BaseModel):
    id: uuid.UUID
    procedure_id: str
    procedure_version: str
    status: str
    status_reason: str | None
    stage: str
    stack_position: int
    in_focus: bool
    parent_task_id: uuid.UUID | None
    owner_instance_id: uuid.UUID | None
    slots: dict[str, TaskSlot]
    pending_confirmation: dict[str, Any] | None
    last_seq: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class TaskList(BaseModel):
    items: list[TaskItem]


class TaskEventItem(BaseModel):
    seq: int
    type: str
    actor: str
    data: dict[str, Any]
    turn_id: uuid.UUID | None
    created_at: datetime


async def _conversacion_del_negocio(
    session: Any, conversation_id: uuid.UUID, tenant_id: uuid.UUID | None
) -> None:
    existe = (
        await session.execute(
            select(Conversation.id).where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == tenant_id,
                Conversation.channel != TEST_CHANNEL,
            )
        )
    ).scalar_one_or_none()
    if existe is None:
        raise ApiError.not_found("Conversación no encontrada.")


def _item(t: ConversationTask) -> TaskItem:
    slots = {
        nombre: TaskSlot(
            value=_visible(meta.get("value"), meta.get("data_class")),
            source=meta.get("source"),
            actor=meta.get("actor"),
            confirmed=bool(meta.get("confirmed")),
            data_class=meta.get("data_class"),
        )
        for nombre, meta in (t.slots or {}).items()
        if isinstance(meta, dict)
    }
    return TaskItem(
        id=t.id,
        procedure_id=t.procedure_id,
        procedure_version=t.procedure_version,
        status=t.status,
        status_reason=t.status_reason,
        stage=t.stage,
        stack_position=t.stack_position,
        in_focus=t.status == "active" and t.stack_position == 0 and t.parent_task_id is None,
        parent_task_id=t.parent_task_id,
        owner_instance_id=t.owner_instance_id,
        slots=slots,
        pending_confirmation=t.pending_confirmation,
        last_seq=t.last_seq,
        created_at=t.created_at,
        updated_at=t.updated_at,
        completed_at=t.completed_at,
    )


def _data_visible(data: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(data or {})
    if "value" in data:
        data["value"] = _visible(data["value"], data.get("data_class"))
    return data


@router.get("/{conversation_id}/tasks", response_model=TaskList)
async def list_tasks(
    conversation_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations"))],
) -> TaskList:
    """Tareas de la conversación: primero las abiertas (en foco y en pausa), luego las cerradas."""
    tenant_id = principal.tenant_id
    async with get_session() as session:
        await _conversacion_del_negocio(session, conversation_id, tenant_id)
        filas = (
            await session.execute(
                select(ConversationTask)
                .where(
                    ConversationTask.tenant_id == tenant_id,
                    ConversationTask.conversation_id == conversation_id,
                )
                .order_by(ConversationTask.created_at.desc())
                .limit(MAX_TAREAS)
            )
        ).scalars().all()
    abiertas = sorted(
        (f for f in filas if f.status in ("active", "suspended")),
        key=lambda f: (f.parent_task_id is not None, f.stack_position, f.created_at),
    )
    cerradas = [f for f in filas if f.status not in ("active", "suspended")]
    return TaskList(items=[_item(f) for f in [*abiertas, *cerradas]])


@router.get("/{conversation_id}/tasks/{task_id}/events", response_model=Page[TaskEventItem])
async def list_task_events(
    conversation_id: uuid.UUID,
    task_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("conversations"))],
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[TaskEventItem]:
    """Historia de la tarea (`task_events`), en orden de `seq`."""
    tenant_id = principal.tenant_id
    async with get_session() as session:
        await _conversacion_del_negocio(session, conversation_id, tenant_id)
        tarea = (
            await session.execute(
                select(ConversationTask.id).where(
                    ConversationTask.id == task_id,
                    ConversationTask.tenant_id == tenant_id,
                    ConversationTask.conversation_id == conversation_id,
                )
            )
        ).scalar_one_or_none()
        if tarea is None:
            raise ApiError.not_found("Tarea no encontrada.")
        filtro = (TaskEvent.tenant_id == tenant_id, TaskEvent.task_id == task_id)
        total = (await session.execute(select(func.count()).select_from(TaskEvent).where(*filtro))).scalar() or 0
        filas = (
            await session.execute(
                select(TaskEvent).where(*filtro).order_by(TaskEvent.seq).offset(params.offset).limit(params.page_size)
            )
        ).scalars().all()
    items = [
        TaskEventItem(seq=e.seq, type=e.type, actor=e.actor, data=_data_visible(e.data), turn_id=e.turn_id,
                      created_at=e.created_at)
        for e in filas
    ]
    return build_page(items, total, params)
