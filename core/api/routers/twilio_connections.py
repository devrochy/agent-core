"""API del alta de WhatsApp vía Twilio (ADR-0186 §3): descubrir los números, conectar uno y ver la conexión.

Solo con la bandera `canales.whatsapp_twilio` del negocio encendida (si no, 404 como un tipo que no existe). Alta y
descubrimiento piden Conexiones en escritura y ser propietario o master: conectan un número que atiende a clientes
reales y guardan credenciales. Leer la conexión pide Conexiones en lectura. El Auth Token no sale en estas
respuestas; solo lo muestra el «revelar» de credenciales (con contraseña y, para Twilio, propietario o master).

Va antes que el router de conexiones en `core/api/__init__.py`: su `/{connection_id}` capturaría `/twilio`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
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
    from core.api.deps import es_owner_o_master

    tid = _tenant(principal)
    await _exigir_bandera(tid)
    conexion = await svc.obtener(tid, connection_id)
    if conexion is None:
        raise ApiError.not_found("Conexión no encontrada.")
    salida = _out(conexion)
    if not es_owner_o_master(principal):
        # Las URL llevan el `public_id` del webhook: solo las ve quien administra la conexión.
        salida = salida.model_copy(update={"webhook_inbound_url": "", "webhook_status_url": ""})
    return salida


# --------------------------------------------------------------------------- operación (PR-5, ADR-0186 §10-11)


class TwilioPruebaIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    to: str = Field(
        min_length=8, max_length=24, description="Número de WhatsApp de destino, en formato +E164."
    )


class TwilioPruebaOut(BaseModel):
    message_sid: str


class TwilioDesconectarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(default=None, description="Contraseña del usuario (confirmación).")
    restore_webhooks: StrictBool = Field(
        default=False,
        description="Devolverle al número de Twilio los webhooks que tenía antes de conectarlo.",
    )
    continuar_si_falla: StrictBool = Field(
        default=False,
        description="Desconectar aunque no se puedan devolver los webhooks (si no, 502 `webhooks_no_restaurados` y "
        "no se borra nada).",
    )


class TwilioDesconectarOut(BaseModel):
    webhooks_restaurados: bool


class TwilioCostoMoneda(BaseModel):
    moneda: str
    mensajes: int
    total: str


class TwilioCostosOut(BaseModel):
    dias: int
    por_moneda: list[TwilioCostoMoneda]
    nota: str = Field(
        description="Twilio no documenta si su precio incluye el cargo de Meta: puede ser solo la tarifa de Twilio."
    )


async def _escritura(principal: Principal, *, bandera_requerida: bool = True) -> uuid.UUID:
    require_owner_or_master(principal)
    tid = _tenant(principal)
    if bandera_requerida:
        await _exigir_bandera(tid)
    return tid


async def _auditar_plantilla(
    principal: Principal,
    accion: str,
    connection_id: uuid.UUID,
    template_id: uuid.UUID | None,
    content_sid: str | None = None,
) -> None:
    """Rastro de una acción sobre las plantillas de Twilio, con cuál fue (no el texto)."""
    from core.shared.audit import record_audit

    datos: dict[str, str] = {"connection_id": str(connection_id)}
    if template_id is not None:
        datos["template_id"] = str(template_id)
    if content_sid:
        datos["content_sid"] = content_sid
    await record_audit(f"admin:{principal.admin.email}", accion, input_data=datos)


async def _auditar(principal: Principal, accion: str, connection_id: uuid.UUID) -> None:
    from core.api.routers.connections import audit_connection_action

    await audit_connection_action(principal, accion, str(connection_id))


def _o_404(conexion: svc.ConexionTwilio | None) -> ConexionTwilioOut:
    if conexion is None:
        raise ApiError.not_found("Conexión no encontrada.")
    return _out(conexion)


@router.post(
    "/{connection_id}/refresh", response_model=ConexionTwilioOut, responses={404: _ERRORES[404]}
)
async def refrescar(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ConexionTwilioOut:
    """Revisa ahora la cuenta, el número y los webhooks contra Twilio (lo mismo que el chequeo de cada 15 min)."""
    from core.gateway.twilio import operacion

    tid = await _escritura(principal)
    return _o_404(await operacion.revisar(tid, connection_id))


@router.post(
    "/{connection_id}/webhooks", response_model=ConexionTwilioOut, responses={404: _ERRORES[404]}
)
async def reregistrar(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ConexionTwilioOut:
    """Vuelve a apuntar los webhooks del número a solvershub (si alguien los cambió en la consola de Twilio)."""
    from core.gateway.twilio import operacion

    tid = await _escritura(principal)
    try:
        resultado = _o_404(await operacion.reregistrar_webhooks(tid, connection_id))
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    await _auditar(principal, "twilio.webhooks_reregistered", connection_id)
    return resultado


@router.post(
    "/{connection_id}/test-message",
    response_model=TwilioPruebaOut,
    responses={
        404: _ERRORES[404],
        409: {"description": "`fuera_de_ventana` o `conexion_pausada`."},
        422: {"description": "`numero_invalido`."},
        429: {"description": "`too_many_tests` (1 cada 30 s) o `daily_limit` (10 al día)."},
    },
)
async def mensaje_de_prueba(
    connection_id: uuid.UUID,
    payload: TwilioPruebaIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TwilioPruebaOut:
    """Manda un texto de prueba. WhatsApp exige que ese número le haya escrito al negocio en las últimas 24 h."""
    from core.gateway.twilio import operacion

    tid = await _escritura(principal)
    try:
        sid = await operacion.enviar_prueba(
            tid, connection_id, payload.to, actor=f"admin:{principal.admin.email}"
        )
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    return TwilioPruebaOut(message_sid=sid)


class TwilioRotarIn(TwilioCredencialesIn):
    password: str | None = Field(default=None, description="Contraseña del usuario (confirmación).")
    confirm_overwrite_webhook: StrictBool = Field(
        default=False,
        description="Si el número ya manda sus mensajes a otra dirección (cambiada en la consola de Twilio), "
        "reemplazarla. Sin esto, la rotación responde 422 `webhook_overwrite_not_confirmed`.",
    )


@router.put("/{connection_id}/credentials", response_model=ConexionTwilioOut, responses=_ERRORES)
async def rotar_credenciales(
    connection_id: uuid.UUID,
    payload: TwilioRotarIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ConexionTwilioOut:
    """Cambia el Auth Token (o la cuenta) del número ya conectado. Pide la contraseña; se valida contra Twilio antes
    de guardar. Una conexión pausada no se rota (409 `conexion_pausada`): rotar no debe reactivarla."""
    from core.api.step_up import confirmar_password
    from core.gateway.twilio import operacion

    tid = await _escritura(principal)
    await confirmar_password(principal, payload.password, proposito="connection_credentials")
    try:
        resultado = _out(
            await operacion.rotar_credenciales(
                tid,
                connection_id,
                account_sid=payload.account_sid,
                auth_token=payload.auth_token,
                confirm_overwrite_webhook=payload.confirm_overwrite_webhook,
            )
        )
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    await _auditar(principal, "twilio.credentials_rotated", connection_id)
    return resultado


@router.post(
    "/{connection_id}/disconnect",
    response_model=TwilioDesconectarOut,
    responses={
        404: _ERRORES[404],
        400: {"description": "`password_required`."},
        403: {"description": "`password_incorrect`."},
    },
)
async def desconectar(
    connection_id: uuid.UUID,
    payload: TwilioDesconectarIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TwilioDesconectarOut:
    """Desconecta el número (borrado lógico, libera el número) y, si se pide, le devuelve sus webhooks anteriores."""
    from core.api.step_up import confirmar_password
    from core.gateway.twilio import operacion

    # Sin exigir la bandera: si se le quita a un negocio, igual tiene que poder desconectar lo que ya tiene.
    tid = await _escritura(principal, bandera_requerida=False)
    await confirmar_password(principal, payload.password, proposito="connection_delete")
    try:
        restaurados = await operacion.desconectar(
            tid,
            connection_id,
            restaurar_webhooks=payload.restore_webhooks,
            continuar_si_falla=payload.continuar_si_falla,
        )
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    await _auditar(principal, "twilio.disconnected", connection_id)
    return TwilioDesconectarOut(webhooks_restaurados=restaurados)


@router.get(
    "/{connection_id}/costs", response_model=TwilioCostosOut, responses={404: _ERRORES[404]}
)
async def costos(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
    dias: Annotated[int, Query(ge=1, le=90)] = 30,
) -> TwilioCostosOut:
    """Lo que Twilio cobró por los mensajes de esta conexión, por moneda."""
    from core.admin.permissions import effective_access
    from core.gateway.twilio import costos as costos_svc

    tid = _tenant(principal)
    await _exigir_bandera(tid)
    # El dinero sale solo con la sección Costos, como en el resto del panel.
    if await effective_access(principal.admin, "costs", tid) is None:
        raise ApiError.forbidden("Sin permiso para la sección 'costs'.")
    if await svc.obtener(tid, connection_id) is None:
        raise ApiError.not_found("Conexión no encontrada.")
    datos = await costos_svc.resumen(tid, connection_id, dias=dias)
    return TwilioCostosOut(
        dias=int(datos["dias"]),  # type: ignore[arg-type]
        por_moneda=[TwilioCostoMoneda(**m) for m in datos["por_moneda"]],  # type: ignore[union-attr]
        nota="Es lo que informa Twilio por cada mensaje. Puede no incluir el cargo de Meta.",
    )


# --------------------------------------------------------------------------- plantillas (PR-6, ADR-0186 §8)


class TwilioPlantillaIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    nombre: str = Field(
        min_length=1, max_length=64, description="Minúsculas, números y guion bajo."
    )
    idioma: str = Field(min_length=2, max_length=5, description="`es`, `es_CO`…")
    categoria: str = Field(description="`UTILITY`, `MARKETING` o `AUTHENTICATION`.")
    cuerpo: str = Field(
        min_length=1, max_length=1024, description="Texto con variables `{{1}}`, `{{2}}`…"
    )
    ejemplos: dict[str, str] = Field(
        default_factory=dict, description='Valor de ejemplo de cada variable: `{"1": "Ana"}`.'
    )


class TwilioPlantillaOut(BaseModel):
    id: uuid.UUID
    connection_id: uuid.UUID
    content_sid: str | None
    nombre: str
    idioma: str
    categoria: str
    cuerpo: str
    ejemplos: dict[str, str]
    estado: str = Field(
        description="`creating`, `unsubmitted`, `received`, `pending`, `approved`, `rejected`, `paused` o `disabled`."
    )
    motivo_rechazo: str
    enviada_at: datetime | None
    aprobada_at: datetime | None


_ERRORES_PLANTILLAS = {
    404: _ERRORES[404],
    409: {"description": "`nombre_en_uso` o `plantilla_sin_crear`."},
    422: {
        "description": "`nombre_invalido`, `idioma_invalido`, `categoria_invalida`, `cuerpo_invalido` o `variables_invalidas`."
    },
    429: {"description": "`sync_rate_limited` (sync manual, uno por minuto)."},
    502: _ERRORES[502],
}


def _plantilla(p: Any) -> TwilioPlantillaOut:
    return TwilioPlantillaOut(**p.__dict__)


async def _plantillas_escritura(principal: Principal, *, solo_dueno: bool = False) -> uuid.UUID:
    """Crear y enviar a aprobación: Conexiones en escritura, como las plantillas de Meta. Sincronizar y borrar actúan
    sobre la cuenta real de Twilio del negocio (incluido lo creado en su consola): además propietario o master."""
    if solo_dueno:
        require_owner_or_master(principal)
    tid = _tenant(principal)
    await _exigir_bandera(tid)
    return tid


@router.get(
    "/{connection_id}/templates",
    response_model=list[TwilioPlantillaOut],
    responses={404: _ERRORES[404]},
)
async def listar_plantillas(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections"))],
    solo_aprobadas: bool = False,
) -> list[TwilioPlantillaOut]:
    """Plantillas de la conexión, con el estado de su aprobación."""
    from core.gateway.twilio import plantillas

    tid = _tenant(principal)
    await _exigir_bandera(tid)
    if await svc.obtener(tid, connection_id) is None:
        raise ApiError.not_found("Conexión no encontrada.")
    return [
        _plantilla(p)
        for p in await plantillas.listar(tid, connection_id, solo_aprobadas=solo_aprobadas)
    ]


@router.post(
    "/{connection_id}/templates",
    response_model=TwilioPlantillaOut,
    status_code=201,
    responses=_ERRORES_PLANTILLAS,
)
async def crear_plantilla(
    connection_id: uuid.UUID,
    payload: TwilioPlantillaIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TwilioPlantillaOut:
    """Crea la plantilla en la cuenta de Twilio (sin enviarla a aprobación). Repetirla tras un corte la reconcilia."""
    from core.gateway.twilio import plantillas

    tid = await _plantillas_escritura(principal)
    try:
        creada = _plantilla(
            await plantillas.crear(
                tid,
                connection_id,
                nombre=payload.nombre,
                idioma=payload.idioma,
                categoria=payload.categoria,
                cuerpo=payload.cuerpo,
                ejemplos=payload.ejemplos,
            )
        )
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    await _auditar_plantilla(
        principal, "twilio.template_created", connection_id, creada.id, creada.content_sid
    )
    return creada


@router.post(
    "/{connection_id}/templates/sync",
    response_model=list[TwilioPlantillaOut],
    responses=_ERRORES_PLANTILLAS,
)
async def sincronizar_plantillas(
    connection_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> list[TwilioPlantillaOut]:
    """Trae todas las plantillas de la cuenta de Twilio (también las creadas en su consola). Uno por minuto."""
    from core.gateway.twilio import plantillas

    tid = await _plantillas_escritura(principal, solo_dueno=True)
    try:
        resultado = [_plantilla(p) for p in await plantillas.sincronizar(tid, connection_id)]
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    await _auditar_plantilla(principal, "twilio.templates_synced", connection_id, None)
    return resultado


@router.post(
    "/{connection_id}/templates/{template_id}/submit",
    response_model=TwilioPlantillaOut,
    responses=_ERRORES_PLANTILLAS,
)
async def enviar_plantilla_a_aprobacion(
    connection_id: uuid.UUID,
    template_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TwilioPlantillaOut:
    """Pide la aprobación de WhatsApp. Idempotente."""
    from core.gateway.twilio import plantillas

    tid = await _plantillas_escritura(principal)
    try:
        enviada = _plantilla(await plantillas.enviar_a_aprobacion(tid, connection_id, template_id))
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    await _auditar_plantilla(
        principal, "twilio.template_submitted", connection_id, template_id, enviada.content_sid
    )
    return enviada


@router.post(
    "/{connection_id}/templates/{template_id}/refresh",
    response_model=TwilioPlantillaOut,
    responses=_ERRORES_PLANTILLAS,
)
async def refrescar_plantilla(
    connection_id: uuid.UUID,
    template_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TwilioPlantillaOut:
    """Consulta ahora el estado de la aprobación."""
    from core.gateway.twilio import plantillas

    tid = await _plantillas_escritura(principal)
    try:
        return _plantilla(await plantillas.refrescar(tid, template_id, connection_id=connection_id))
    except svc.AltaError as exc:
        raise _api_error(exc) from None


@router.delete(
    "/{connection_id}/templates/{template_id}", status_code=204, responses={404: _ERRORES[404]}
)
async def borrar_plantilla(
    connection_id: uuid.UUID,
    template_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Borra la plantilla (también en Twilio, si se puede)."""
    from core.gateway.twilio import plantillas

    tid = await _plantillas_escritura(principal, solo_dueno=True)
    try:
        await plantillas.borrar(tid, connection_id, template_id)
    except svc.AltaError as exc:
        raise _api_error(exc) from None
    await _auditar_plantilla(principal, "twilio.template_deleted", connection_id, template_id)
    return Response(status_code=204)
