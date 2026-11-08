"""API del alta de WhatsApp vía Twilio (ADR-0186 §3): descubrir los números, conectar uno y ver la conexión.

Solo con la bandera `canales.whatsapp_twilio` del negocio encendida (si no, 404 como un tipo que no existe). Alta y
descubrimiento piden Conexiones en escritura y ser propietario o master: conectan un número que atiende a clientes
reales y guardan credenciales. Leer la conexión pide Conexiones en lectura. El Auth Token no sale en estas
respuestas; solo lo muestra el «revelar» de credenciales (con contraseña y, para Twilio, propietario o master).

Va antes que el router de conexiones en `core/api/__init__.py`: su `/{connection_id}` capturaría `/twilio`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError
from core.db.session import get_session
from core.gateway.twilio import alta as svc
from core.gateway.twilio import bandera

router = APIRouter(prefix="/admin/connections/twilio", tags=["connections"])

_CANAL = "whatsapp_twilio"
_ERRORES = {
    404: {"description": "Twilio no está habilitado para este negocio, o la conexión no existe."},
    409: {
        "description": "`sender_ya_conectado`, `cuenta_en_otro_negocio`, `twilio_account_suspended` o `conflicto`."
    },
    422: {
        "description": "`twilio_credentials_invalid`, `sender_not_found`, `sender_not_online` o "
        "`webhook_overwrite_not_confirmed` (con `details.previous_webhook`)."
    },
    502: {"description": "`twilio_unavailable` o `twilio_error`."},
}


class TwilioCredencialesIn(BaseModel):
    # Un SID pegado con un espacio al final se limpia antes de validar el largo, en vez de rechazarse.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    account_sid: str = Field(
        min_length=34, max_length=34, description="Account SID de Twilio (`AC…`)."
    )
    auth_token: str = Field(
        min_length=1, max_length=128, description="Auth Token de la cuenta. No se devuelve nunca."
    )


class TwilioAltaIn(TwilioCredencialesIn):
    sender_sid: str = Field(
        min_length=34, max_length=34, description="SID del sender elegido (`XE…`)."
    )
    label: str = Field(default="", max_length=120)
    confirm_overwrite_webhook: StrictBool = Field(
        default=False,
        description="Reemplazar los webhooks que el sender ya tenía hacia otra dirección (se guardan para "
        "restaurarlos al desconectar).",
    )


class TwilioCuentaOut(BaseModel):
    friendly_name: str
    status: str
    type: str


class TwilioSenderOut(BaseModel):
    sender_sid: str
    e164: str
    status: str
    display_name: str
    quality_rating: str
    seleccionable: bool
    motivo: str = Field(description='`""`, `no_online` o `ya_conectado_aqui`.')
    webhook_ajeno: bool = Field(description="El sender ya envía sus mensajes a otra dirección.")


class TwilioDescubrirOut(BaseModel):
    account: TwilioCuentaOut
    senders: list[TwilioSenderOut]


class ConexionTwilioOut(BaseModel):
    connection_id: uuid.UUID
    label: str
    sender_e164: str
    sender_sid: str
    sender_status: str
    display_name: str
    status: str = Field(description="`pending`, `connected`, `degraded`, `revoked` o `error`.")
    account_sid: str
    webhook_inbound_url: str
    webhook_status_url: str
    tenia_webhook_previo: bool
    last_error_code: int | None
    active: bool


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


async def _exigir_bandera(tenant_id: uuid.UUID) -> None:
    async with get_session() as session:
        if not await bandera.habilitado(session, tenant_id, _CANAL):
            raise ApiError.not_found("WhatsApp vía Twilio no está disponible para este negocio.")


def _api_error(exc: svc.AltaError) -> ApiError:
    return ApiError(exc.http_status, exc.code, exc.message, details=exc.details)


def _out(c: svc.ConexionTwilio) -> ConexionTwilioOut:
    return ConexionTwilioOut(
        connection_id=c.connection_id,
        label=c.label,
        sender_e164=c.sender_e164,
        sender_sid=c.sender_sid,
        sender_status=c.sender_status,
        display_name=c.display_name,
        status=c.status,
        account_sid=c.account_sid,
        webhook_inbound_url=c.webhook_inbound_url,
        webhook_status_url=c.webhook_status_url,
        tenia_webhook_previo=c.tenia_webhook_previo,
        last_error_code=c.last_error_code,
        active=c.active,
    )


@router.post("/discover", response_model=TwilioDescubrirOut, responses=_ERRORES)
async def descubrir(
    payload: TwilioCredencialesIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TwilioDescubrirOut:
    """Valida las credenciales contra Twilio y lista los números de WhatsApp de la cuenta. No guarda nada."""
    require_owner_or_master(principal)
    tid = _tenant(principal)
    await _exigir_bandera(tid)
    try:
        cuenta, senders = await svc.descubrir(tid, payload.account_sid, payload.auth_token)
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    return TwilioDescubrirOut(
        account=TwilioCuentaOut(
            friendly_name=cuenta.friendly_name, status=cuenta.status, type=cuenta.type
        ),
        senders=[
            TwilioSenderOut(
                sender_sid=s.sender_sid,
                e164=s.e164,
                status=s.status,
                display_name=s.display_name,
                quality_rating=s.quality_rating,
                seleccionable=s.seleccionable,
                motivo=s.motivo,
                webhook_ajeno=s.webhook_ajeno,
            )
            for s in senders
        ],
    )


@router.post(
    "",
    response_model=ConexionTwilioOut,
    status_code=201,
    responses={
        **_ERRORES,
        200: {"description": "El número ya estaba conectado en este negocio: se actualizó."},
    },
)
async def conectar(
    payload: TwilioAltaIn,
    response: Response,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ConexionTwilioOut:
    """Conecta el número elegido: guarda la conexión, registra los webhooks del sender en Twilio y los relee.
    201 si se creó; 200 si el número ya estaba conectado en este negocio. Si registrar los webhooks falla, la
    conexión queda en `error` (con `last_error_code`) y se reintenta con el mismo pedido."""
    require_owner_or_master(principal)
    tid = _tenant(principal)
    await _exigir_bandera(tid)
    try:
        resultado = await svc.conectar(
            tid,
            account_sid=payload.account_sid,
            auth_token=payload.auth_token,
            sender_sid=payload.sender_sid,
            label=payload.label,
            confirm_overwrite_webhook=payload.confirm_overwrite_webhook,
        )
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    if not resultado.created:
        response.status_code = 200
    return _out(resultado)


@router.get("/{connection_id}", response_model=ConexionTwilioOut, responses={404: _ERRORES[404]})
async def ver(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> ConexionTwilioOut:
    """Resumen de una conexión de Twilio del negocio: número, estado y webhooks registrados."""
    tid = _tenant(principal)
    await _exigir_bandera(tid)
    conexion = await svc.obtener(tid, connection_id)
    if conexion is None:
        raise ApiError.not_found("Conexión no encontrada.")
    return _out(conexion)
