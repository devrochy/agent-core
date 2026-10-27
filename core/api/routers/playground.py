"""Playground: probar el asistente del tenant sin pasar por ningún canal.

Migrado del panel clásico (`/admin/asistente/probar`). Reutiliza
`core.admin.playground`, que corre el MISMO loop del runtime sobre una
conversación sintética (canal `test`): no toca Meta/waweb, no persiste mensajes
ni consume métricas.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

router = APIRouter(prefix="/admin/playground", tags=["admin · playground"])


class TurnIn(BaseModel):
    text: str
    connection_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Conexión del negocio por cuya release se prueba (HU-AOS-57): el turno corre por el agente "
            "publicado ahí. Sin ella, la prueba de siempre (sin release). Sigue siendo del canal `test`."
        ),
    )


class TurnOut(BaseModel):
    text: str
    image_url: str | None = None
    escalated: bool = False


class HistoryOut(BaseModel):
    turns: list[dict[str, Any]]


def _conexion_no_valida(exc: Exception) -> ApiError:
    return ApiError(422, "invalid_connection", str(exc), fields={"connection_id": str(exc)})


@router.get("", response_model=HistoryOut)
async def playground_history(
    principal: Annotated[Principal, Depends(require("connections"))],
    connection_id: Annotated[uuid.UUID | None, Query(description="Historial de la prueba por esa conexión.")] = None,
) -> HistoryOut:
    """Historial de la prueba (working memory del canal `test`)."""
    from core.admin import playground
    from core.gateway.conexion_pruebas import ConexionNoValida

    turns: list[dict[str, Any]] = []
    async with tenant_scope(principal.tenant_id):
        try:
            conversation = await playground.get_or_create_test_conversation(principal.tenant_id, connection_id)
            turns = await playground.list_test_turns(conversation.id)
        except ConexionNoValida as exc:
            raise _conexion_no_valida(exc) from exc
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
    from core.gateway.conexion_pruebas import ConexionNoValida

    async with tenant_scope(principal.tenant_id):
        try:
            turn = await playground.run_test_turn(principal.tenant_id, text, payload.connection_id)
        except ConexionNoValida as exc:
            raise _conexion_no_valida(exc) from exc
        except playground.PlaygroundError as exc:
            raise ApiError.bad_request(str(exc), "playground_error") from exc
        # Solo cuenta si el agente contestó: un turno que escaló por falla del modelo no prueba nada.
        if payload.connection_id is not None and not turn.escalated:
            await _marcar_prueba_del_onboarding(principal.tenant_id, payload.connection_id)
    return TurnOut(text=turn.text, image_url=turn.image_url, escalated=turn.escalated)


@router.post("/reset", status_code=204)
async def playground_reset(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
    connection_id: Annotated[uuid.UUID | None, Query(description="Reinicia la prueba de esa conexión.")] = None,
) -> Response:
    """Cierra la prueba actual y arranca una nueva desde cero."""
    from core.admin import playground
    from core.gateway.conexion_pruebas import ConexionNoValida

    async with tenant_scope(principal.tenant_id):
        try:
            await playground.reset_test_conversation(principal.tenant_id, connection_id)
        except ConexionNoValida as exc:
            raise _conexion_no_valida(exc) from exc
    return Response(status_code=204)


async def _marcar_prueba_del_onboarding(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Si la prueba fue por la conexión donde el onboarding dejó listo el agente, queda anotada la
    primera vez (`finalizar` la exige). Mejor esfuerzo: una falla aquí no estropea la respuesta."""
    from core.primeros_pasos import recorrido

    try:
        await recorrido.marcar_prueba(tenant_id, connection_id)
    except Exception:
        log.warning("onboarding_marcar_prueba_fallo", tenant_id=str(tenant_id), exc_info=True)
