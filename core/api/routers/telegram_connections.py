"""API del canal Telegram (ADR-0199): conectar el bot del negocio, verlo y operarlo.

Para todos los negocios, sin bandera por negocio; con el kill-switch del canal apagado responde 404 como un tipo que
no existe (salvo desconectar: lo que ya está conectado se tiene que poder quitar). Conectar y operar piden Conexiones
en escritura y ser propietario o master: conectan un bot que atiende a clientes reales y guardan su token. Leer pide
Conexiones en lectura. El token no sale nunca en estas respuestas.

Los nombres de las rutas siguen a los de Twilio para reutilizar el BFF y la ficha del panel. Va antes que el router de
conexiones en `core/api/__init__.py`: su `/{connection_id}` capturaría `/telegram`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError
from core.gateway.telegram import alta as svc

router = APIRouter(prefix="/admin/connections/telegram", tags=["connections"])

_ERRORES = {
    404: {"description": "Telegram no está disponible, o la conexión no existe."},
    409: {
        "description": "`bot_en_otro_negocio`, `webhook_ajeno` (con `details.webhook_url`), `conexion_pausada` o "
        "`conflicto`."
    },
    422: {"description": "`telegram_token_invalido`."},
    502: {
        "description": "`telegram_unavailable`, `telegram_error` o `telegram_webhook_no_registrado` (la conexión "
        "quedó guardada en `details.connection_id`; repetir el pedido es idempotente)."
    },
    503: {"description": "`url_publica_no_https`: el ambiente no tiene una URL pública https."},
}


class TelegramTokenIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    bot_token: str = Field(
        min_length=20,
        max_length=160,
        description="Token del bot que da @BotFather (`123456789:AAH…`). No se devuelve nunca.",
    )
    confirm_overwrite_webhook: StrictBool = Field(
        default=False,
        description="Reemplazar el webhook que el bot ya tenía hacia otra dirección (otra integración).",
    )


class TelegramAltaIn(TelegramTokenIn):
    label: str = Field(default="", max_length=120)


class TelegramRotarIn(TelegramTokenIn):
    password: str | None = Field(default=None, description="Contraseña del usuario (confirmación).")


class ConexionTelegramOut(BaseModel):
    connection_id: uuid.UUID
    label: str
    bot_id: str = Field(description="Id del bot en Telegram (de `getMe`).")
    bot_username: str = Field(description="`@usuario` del bot, sin la arroba.")
    bot_name: str
    status: str = Field(description="`pending`, `connected`, `degraded`, `revoked` o `error`.")
    webhook_url: str = Field(description='URL registrada en el bot (`""` para quien no administra la conexión).')
    webhook_confirmado_at: datetime | None
    pending_update_count: int = Field(description="Updates que Telegram tiene esperando entrega.")
    last_error_message: str | None
    last_error_at: datetime | None
    last_update_at: datetime | None
    last_checked_at: datetime | None
    active: bool


class TelegramPruebaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: int = Field(
        description="Id del chat de Telegram de destino. Tiene que haberle escrito al bot antes (/start)."
    )


class TelegramPruebaOut(BaseModel):
    message_id: int


class TelegramDesconectarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(default=None, description="Contraseña del usuario (confirmación).")
    continuar_si_falla: StrictBool = Field(
        default=False,
        description="Desconectar aunque no se pueda quitar el webhook del bot (si no, 502 `webhook_no_eliminado` y "
        "no se borra nada).",
    )


class TelegramDesconectarOut(BaseModel):
    webhook_quitado: bool


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


def _exigir_habilitado() -> None:
    try:
        svc.exigir_habilitado()
    except svc.AltaError as exc:
        raise _api_error(exc) from None


def _api_error(exc: svc.AltaError) -> ApiError:
    return ApiError(exc.http_status, exc.code, exc.message, details=exc.details)


def _out(c: svc.ConexionTelegram, *, con_url: bool = True) -> ConexionTelegramOut:
    return ConexionTelegramOut(
        connection_id=c.connection_id,
        label=c.label,
        bot_id=c.bot_id,
        bot_username=c.bot_username,
        bot_name=c.bot_name,
        status=c.status,
        # La URL lleva el `public_id` del webhook: solo la ve quien administra la conexión.
        webhook_url=c.webhook_url if con_url else "",
        webhook_confirmado_at=c.webhook_confirmado_at,
        pending_update_count=c.pending_update_count,
        last_error_message=c.last_error_message,
        last_error_at=c.last_error_at,
        last_update_at=c.last_update_at,
        last_checked_at=c.last_checked_at,
        active=c.active,
    )


def _o_404(conexion: svc.ConexionTelegram | None) -> ConexionTelegramOut:
    if conexion is None:
        raise ApiError.not_found("Conexión no encontrada.")
    return _out(conexion)


async def _escritura(principal: Principal, *, habilitado_requerido: bool = True) -> uuid.UUID:
    require_owner_or_master(principal)
    tid = _tenant(principal)
    if habilitado_requerido:
        _exigir_habilitado()
    return tid


async def _auditar(principal: Principal, accion: str, connection_id: uuid.UUID) -> None:
    from core.api.routers.connections import audit_connection_action

    await audit_connection_action(principal, accion, str(connection_id))


@router.post(
    "",
    response_model=ConexionTelegramOut,
    status_code=201,
    responses={**_ERRORES, 200: {"description": "El bot ya estaba conectado en este negocio: se actualizó."}},
)
async def conectar(
    payload: TelegramAltaIn,
    response: Response,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ConexionTelegramOut:
    """Conecta el bot: valida el token con `getMe`, guarda la conexión, registra el webhook con un secreto propio y
    lo confirma con `getWebhookInfo`. 201 si se creó; 200 si el bot ya estaba conectado en este negocio."""
    tid = await _escritura(principal)
    try:
        resultado = await svc.conectar(
            tid,
            bot_token=payload.bot_token,
            label=payload.label,
            confirm_overwrite_webhook=payload.confirm_overwrite_webhook,
        )
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    await _auditar(principal, "telegram.connected", resultado.connection_id)
    if not resultado.created:
        response.status_code = 200
    return _out(resultado)


@router.get("", response_model=list[ConexionTelegramOut], responses={404: _ERRORES[404]})
async def listar(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> list[ConexionTelegramOut]:
    """Los bots de Telegram conectados en el negocio."""
    from core.api.deps import es_owner_o_master

    tid = _tenant(principal)
    _exigir_habilitado()
    con_url = es_owner_o_master(principal)
    return [_out(c, con_url=con_url) for c in await svc.listar(tid)]


@router.get("/{connection_id}", response_model=ConexionTelegramOut, responses={404: _ERRORES[404]})
async def ver(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> ConexionTelegramOut:
    """Resumen de un bot conectado: usuario, estado, webhook y lo último que dijo Telegram."""
    from core.api.deps import es_owner_o_master

    tid = _tenant(principal)
    _exigir_habilitado()
    conexion = await svc.obtener(tid, connection_id)
    if conexion is None:
        raise ApiError.not_found("Conexión no encontrada.")
    return _out(conexion, con_url=es_owner_o_master(principal))


@router.post(
    "/{connection_id}/refresh", response_model=ConexionTelegramOut, responses={404: _ERRORES[404]}
)
async def refrescar(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ConexionTelegramOut:
    """Revisa ahora el token y el webhook contra Telegram (lo mismo que el chequeo de cada 15 min)."""
    from core.gateway.telegram import operacion

    tid = await _escritura(principal)
    return _o_404(await operacion.revisar(tid, connection_id))


@router.post(
    "/{connection_id}/webhooks",
    response_model=ConexionTelegramOut,
    responses={404: _ERRORES[404], 409: {"description": "`conexion_pausada`."}, 503: _ERRORES[503]},
)
async def reparar(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ConexionTelegramOut:
    """Vuelve a apuntar el webhook del bot a solvershub (si otro sistema lo cambió)."""
    from core.gateway.telegram import operacion

    tid = await _escritura(principal)
    try:
        resultado = _o_404(await operacion.reparar_webhook(tid, connection_id))
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    await _auditar(principal, "telegram.webhook_repaired", connection_id)
    return resultado


@router.post(
    "/{connection_id}/test-message",
    response_model=TelegramPruebaOut,
    responses={
        404: _ERRORES[404],
        409: {"description": "`chat_no_iniciado`, `token_revocado` o `conexion_pausada`."},
        429: {"description": "`too_many_tests` (1 cada 30 s) o `daily_limit` (10 al día)."},
        502: {"description": "`telegram_unavailable` o `telegram_error`."},
    },
)
async def mensaje_de_prueba(
    connection_id: uuid.UUID,
    payload: TelegramPruebaIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TelegramPruebaOut:
    """Manda un texto de prueba a un chat que ya le escribió al bot."""
    from core.gateway.telegram import operacion

    tid = await _escritura(principal)
    try:
        message_id = await operacion.enviar_prueba(
            tid, connection_id, payload.chat_id, actor=f"admin:{principal.admin.email}"
        )
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    return TelegramPruebaOut(message_id=message_id)


@router.put(
    "/{connection_id}/credentials",
    response_model=ConexionTelegramOut,
    responses={
        **_ERRORES,
        409: {"description": "`otro_bot` (el token es de otro bot), `conexion_pausada` o `webhook_ajeno`."},
    },
)
async def rotar_credenciales(
    connection_id: uuid.UUID,
    payload: TelegramRotarIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ConexionTelegramOut:
    """Cambia el token del mismo bot (tras revocarlo en @BotFather). Pide la contraseña; se valida contra Telegram
    antes de guardar. Conserva el `public_id` y rota el secreto del webhook."""
    from core.api.step_up import confirmar_password
    from core.gateway.telegram import operacion

    tid = await _escritura(principal)
    await confirmar_password(principal, payload.password, proposito="connection_credentials")
    try:
        resultado = _out(
            await operacion.rotar_credenciales(
                tid,
                connection_id,
                bot_token=payload.bot_token,
                confirm_overwrite_webhook=payload.confirm_overwrite_webhook,
            )
        )
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    await _auditar(principal, "telegram.credentials_rotated", connection_id)
    return resultado


@router.post(
    "/{connection_id}/disconnect",
    response_model=TelegramDesconectarOut,
    responses={
        404: {"description": "La conexión no existe."},
        400: {"description": "`password_required`."},
        403: {"description": "`password_incorrect`."},
        502: {"description": "`webhook_no_eliminado`."},
    },
)
async def desconectar(
    connection_id: uuid.UUID,
    payload: TelegramDesconectarIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TelegramDesconectarOut:
    """Quita el webhook del bot y borra lógicamente la conexión (libera el bot para otro negocio)."""
    from core.api.step_up import confirmar_password
    from core.gateway.telegram import operacion

    # Sin exigir el kill-switch: lo que ya está conectado se tiene que poder quitar.
    tid = await _escritura(principal, habilitado_requerido=False)
    await confirmar_password(principal, payload.password, proposito="connection_delete")
    try:
        quitado = await operacion.desconectar(
            tid, connection_id, continuar_si_falla=payload.continuar_si_falla
        )
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    await _auditar(principal, "telegram.disconnected", connection_id)
    return TelegramDesconectarOut(webhook_quitado=quitado)
