"""Playground: probar el asistente del tenant sin pasar por ningún canal.

Migrado del panel clásico (`/admin/asistente/probar`). Reutiliza
`core.admin.playground`, que corre el MISMO loop del runtime sobre una
conversación sintética (canal `test`): no toca Meta/waweb, no persiste mensajes
ni consume métricas.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.shared.tenant import tenant_scope

router = APIRouter(prefix="/admin/playground", tags=["admin · playground"])


class TurnIn(BaseModel):
    text: str


class TurnOut(BaseModel):
    text: str
    image_url: str | None = None
    escalated: bool = False


class HistoryOut(BaseModel):
    turns: list[dict[str, Any]]


@router.get("", response_model=HistoryOut)
async def playground_history(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> HistoryOut:
    """Historial de la prueba (working memory del canal `test`)."""
    from core.admin import playground

    turns: list[dict[str, Any]] = []
    async with tenant_scope(principal.tenant_id):
        try:
            conversation = await playground.get_or_create_test_conversation(principal.tenant_id)
            turns = await playground.list_test_turns(conversation.id)
        except Exception:
            # Redis caído o spec ausente: el panel muestra el historial vacío.
            turns = []
    return HistoryOut(turns=turns)


@router.post("/message", response_model=TurnOut)
async def playground_message(
    payload: TurnIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TurnOut:
    """Procesa un turno contra el loop real y devuelve la respuesta del asistente."""
    text = payload.text.strip()
    if not text:
        raise ApiError.bad_request("Escribe un mensaje.", "missing_text")
    from core.admin import playground

    async with tenant_scope(principal.tenant_id):
        try:
            turn = await playground.run_test_turn(principal.tenant_id, text)
        except playground.PlaygroundError as exc:
            raise ApiError.bad_request(str(exc), "playground_error") from exc
    return TurnOut(text=turn.text, image_url=turn.image_url, escalated=turn.escalated)


@router.post("/reset", status_code=204)
async def playground_reset(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Cierra la prueba actual y arranca una nueva desde cero."""
    from core.admin import playground

    async with tenant_scope(principal.tenant_id):
        await playground.reset_test_conversation(principal.tenant_id)
    return Response(status_code=204)
