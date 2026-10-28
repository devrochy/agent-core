"""Playground: probar el asistente del tenant sin pasar por ningún canal.

Migrado del panel clásico (`/admin/asistente/probar`). Reutiliza
`core.admin.playground`, que corre el MISMO loop del runtime sobre una
conversación sintética (canal `test`): no toca Meta/waweb, no persiste mensajes
ni consume métricas.

Opciones directas (SPEC-40, ADR-0172): `POST /options` envía una plantilla con opciones a la conversación de
prueba y `POST /tap` simula el toque de un botón, sin Meta. El payload firmado se valida igual que en
producción (firma por negocio y conversación, `option_sends`, vigencia, una sola ejecución): ningún camino se
salta el resolver. **La tool se ejecuta DE VERDAD contra los datos del negocio**: usar un agente con datos de
prueba (SPEC-32). Permisos como el editor de plantillas con opciones (dueño o master, escritura en
`connections`) y con la bandera `opciones_directas.habilitada` encendida (si no, 404).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.routers.plantillas_opciones import OpcionesErrorOut
from core.api.routers.plantillas_opciones import escritura as _escritura_opciones
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


class PlaygroundOpcionesIn(BaseModel):
    clave: str = Field(description="Clave de la plantilla con opciones (versión vigente publicada).")
    connection_id: uuid.UUID | None = Field(
        default=None, description="Conexión por cuya release se prueba, como en `/message`. Sin ella, la de siempre."
    )


class PlaygroundToqueIn(BaseModel):
    payload: str = Field(
        max_length=256,
        description="`id` de un botón del mensaje (`opt:…`, `optc:…` o `optn:…`), tal cual lo devolvió `/options` o `/tap`.",
    )
    connection_id: uuid.UUID | None = Field(default=None, description="La misma conexión con la que se envió.")


class PlaygroundMensajeOut(BaseModel):
    id: uuid.UUID
    direction: str = Field(description="`outbound` (lo que el sistema le dijo al cliente de prueba) o `inbound`.")
    content: str
    interactive: dict[str, Any] | None = Field(
        default=None, description="Botones o lista del mensaje (payload de WhatsApp) con los ids firmados que `/tap` recibe."
    )
    created_at: datetime


class PlaygroundOpcionesOut(BaseModel):
    enviado: bool
    ya_enviado: bool = False
    via: Literal["interactivo", "texto"] | None = Field(default=None, description="`interactivo` (botones) o `texto`.")
    ejecucion_directa: bool
    degradado: bool
    send_id: uuid.UUID | None = None
    mensajes: list[PlaygroundMensajeOut] = Field(description="Los mensajes del envío, listos para pintar.")


class PlaygroundToqueOut(BaseModel):
    outcome: str = Field(
        description=(
            "Resultado del resolver (`ok`, `tool_error`, `denied`, `confirm_requested`, `cancelled`, `expired`, "
            "`duplicate`, `bad_signature`, `unknown_send`, `foreign_conversation`, …). Un payload manipulado, de otro "
            "negocio o de otra conversación se descarta sin mensajes: `mensajes` vacío."
        )
    )
    mensajes: list[PlaygroundMensajeOut] = Field(description="Lo que el toque produjo: respuesta fija o botones de confirmación.")


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


def _mensaje_out(m: Any) -> PlaygroundMensajeOut:
    return PlaygroundMensajeOut(
        id=m.id, direction=m.direction, content=m.content or "", interactive=m.interactive, created_at=m.created_at
    )


# Errores del sobre `{error: {code, message}}`: 404 (bandera apagada o clave inexistente), 409 (no publicada, sin
# plantilla, pausada) y 422 (contexto incompleto o `payload_invalido`).
_ERRORES_OPCIONES: dict[int | str, dict[str, Any]] = {
    404: {"model": OpcionesErrorOut},
    409: {"model": OpcionesErrorOut},
    422: {"model": OpcionesErrorOut},
}


@router.post("/options", response_model=PlaygroundOpcionesOut, responses=_ERRORES_OPCIONES)
async def playground_options(
    payload: PlaygroundOpcionesIn,
    principal: Annotated[Principal, Depends(_escritura_opciones)],
) -> PlaygroundOpcionesOut:
    """Envía la plantilla con opciones `clave` a la conversación de prueba, con `send_options_to_conversation`: crea el
    `option_sends` con sus payloads firmados y guarda el mensaje con los botones, sin Meta ni cola. Los errores son
    los de `POST /plantillas-opciones/{clave}/enviar-prueba` (404 si la bandera está apagada o la clave no existe,
    409 si no está publicada, 422 si falta contexto)."""
    from core.admin import plantillas_opciones_service as svc
    from core.admin import playground
    from core.gateway.conexion_pruebas import ConexionNoValida

    async with tenant_scope(principal.tenant_id):
        try:
            r, mensajes = await playground.enviar_opciones(
                principal.tenant_id, f"admin:{principal.admin.id}", payload.clave, payload.connection_id
            )
        except ConexionNoValida as exc:
            raise _conexion_no_valida(exc) from exc
        except svc.ErrorPlantillaOpciones as exc:
            raise ApiError(exc.status, exc.code, exc.message, details=exc.details) from exc
    return PlaygroundOpcionesOut(**r, mensajes=[_mensaje_out(m) for m in mensajes])


@router.post("/tap", response_model=PlaygroundToqueOut, responses=_ERRORES_OPCIONES)
async def playground_tap(
    payload: PlaygroundToqueIn,
    principal: Annotated[Principal, Depends(_escritura_opciones)],
) -> PlaygroundToqueOut:
    """Simula el toque de un botón de la conversación de prueba con el MISMO resolver que atiende WhatsApp: firma
    por negocio y conversación, vigencia, una sola ejecución y autorización de la tool, sin LLM. Devuelve el
    `outcome` y los mensajes que produjo. **Ejecuta la tool de verdad.** Un payload que no es de opciones es 422."""
    from core.admin import playground
    from core.gateway.conexion_pruebas import ConexionNoValida
    from core.kernel.opciones.payload import es_de_opciones

    ficha = payload.payload.strip()
    if not es_de_opciones(ficha):
        raise ApiError(422, "payload_invalido", "No es el botón de una plantilla con opciones.")
    async with tenant_scope(principal.tenant_id):
        try:
            outcome, nuevos = await playground.tocar_opcion(principal.tenant_id, ficha, payload.connection_id)
        except ConexionNoValida as exc:
            raise _conexion_no_valida(exc) from exc
        except playground.PlaygroundError as exc:
            raise ApiError(404, "conversacion_no_encontrada", str(exc)) from exc
    return PlaygroundToqueOut(outcome=outcome, mensajes=[_mensaje_out(m) for m in nuevos])


async def _marcar_prueba_del_onboarding(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Si la prueba fue por la conexión donde el onboarding dejó listo el agente, queda anotada la
    primera vez (`finalizar` la exige). Mejor esfuerzo: una falla aquí no estropea la respuesta."""
    from core.primeros_pasos import recorrido

    try:
        await recorrido.marcar_prueba(tenant_id, connection_id)
    except Exception:
        log.warning("onboarding_marcar_prueba_fallo", tenant_id=str(tenant_id), exc_info=True)
