"""Wizard de alta de WhatsApp en la API (ADR-0080; Embedded Signup: ADR-0128).

Reutiliza los mismos servicios que el panel clásico: `core.gateway.meta_onboarding`
(estado del wizard, exchange del Embedded Signup, registro de número y PIN),
`core.gateway.meta_byok` (alta con app propia) y la carga manual como escape.
Las credenciales de Meta (App ID/Secret/Config ID) se leen del entorno; sin ellas
las pantallas y los errores siguen funcionando, pero el flujo real no se completa.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, SecretStr

from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError
from core.config import get_settings
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

router = APIRouter(prefix="/admin/meta", tags=["admin · wizard whatsapp"])


class WizardState(BaseModel):
    """Qué puede ofrecer el wizard en este entorno. `missing_settings`: las variables de
    entorno de la app de la plataforma que faltan para Embedded Signup (vacío si está lista).
    `graph_api_version`: la versión con la que el panel inicializa el SDK de Facebook."""

    meta_configured: bool
    app_id: str
    config_id: str
    environments: list[str]
    graph_api_version: str = ""
    missing_settings: list[str] = []


@router.get("/wizard", response_model=WizardState)
async def wizard_state(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardState:
    """Qué puede ofrecer el wizard en este entorno (app de Meta configurada o no). El App ID y
    el `config_id` son públicos (los usa el SDK de Facebook en el navegador); el App Secret
    nunca sale del servidor."""
    from core.gateway.meta_onboarding import missing_platform_settings

    settings = get_settings()
    faltan = missing_platform_settings()
    return WizardState(
        meta_configured=not faltan,
        app_id=settings.meta_app_id or "",
        config_id=settings.meta_config_id or "",
        environments=["sandbox", "production"],
        graph_api_version=settings.graph_api_version,
        missing_settings=faltan,
    )


Entorno = Literal["sandbox", "production"]


class WizardErrorOut(BaseModel):
    """Último error del wizard, sin nada sensible: código, mensaje y paso. `fbtrace_id` es el
    identificador de Meta para soporte."""

    code: str | None = None
    message: str | None = None
    at_step: str | None = None
    fbtrace_id: str | None = None


class WizardCancel(BaseModel):
    current_step: str | None = None
    with_error: bool = False
    at: str | None = None


class WizardSessionOut(BaseModel):
    """Estado del alta con Facebook (Embedded Signup). `attempt`: solo al abrir o reanudar una
    sesión que todavía espera el popup; el panel lo devuelve con el `code` (un solo uso).
    `needs_pin`: el paso que falta es registrar el número con su PIN."""

    wizard_id: uuid.UUID
    step: str
    connection_id: uuid.UUID | None = None
    environment: str = "production"
    completed_steps: list[str] = []
    done: bool = False
    expired: bool = False
    expires_at: datetime | None = None
    needs_pin: bool = False
    last_error: WizardErrorOut | None = None
    cancelled: WizardCancel | None = None
    attempt: str | None = None
    connection_status: str | None = None
    connection_status_reason: str | None = None


async def _view_out(view: Any, *, attempt: str | None = None) -> WizardSessionOut:
    from core.db.models import MetaConnection, utcnow

    status = reason = None
    if view.connection_id is not None:
        async with get_session() as session:
            connection = await session.get(MetaConnection, view.connection_id)
            if connection is not None and connection.tenant_id == get_current_tenant_id():
                status, reason = connection.status, connection.status_reason
    error = view.last_error or None
    return WizardSessionOut(
        wizard_id=view.id,
        step=view.current_step,
        connection_id=view.connection_id,
        environment=view.environment,
        completed_steps=list(view.completed_steps or []),
        done=view.current_step == "done",
        expired=view.current_step != "done" and view.expires_at is not None and view.expires_at <= utcnow(),
        expires_at=view.expires_at,
        needs_pin=view.current_step == "register_number",
        last_error=WizardErrorOut(
            code=None if error.get("code") is None else str(error.get("code")),
            message=error.get("message"),
            at_step=error.get("at_step"),
            fbtrace_id=error.get("fbtrace_id"),
        ) if error else None,
        cancelled=WizardCancel(**{k: view.cancelled.get(k) for k in ("current_step", "with_error", "at")})
        if view.cancelled
        else None,
        attempt=attempt,
        connection_status=status,
        connection_status_reason=reason,
    )


def _wizard_api_error(exc: Any) -> ApiError:
    status = getattr(exc, "status", 400)
    message = str(getattr(exc, "message", None) or exc)[:200]
    code = getattr(exc, "code", "wizard_error")
    if status == 404:
        return ApiError.not_found(message)
    if status == 409:
        return ApiError(409, code, message)
    return ApiError.bad_request(message, code)


def _negocio(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Elige un negocio.", "tenant_required")
    return principal.tenant_id


def _guard_production(principal: Principal, environment: str) -> None:
    """El alta de producción conecta el número que atiende a clientes reales: como BYOK de
    producción y registrar el webhook, solo el propietario del negocio o el master
    (ADR-0127)."""
    if environment == "production":
        require_owner_or_master(principal)


async def _wizard_env(wizard_id: uuid.UUID) -> str:
    """Entorno del wizard, o 404 si no es de este negocio (antes de cualquier otra regla)."""
    from core.gateway.meta_onboarding import WizardError, get_wizard

    try:
        wizard = await get_wizard(wizard_id)
    except WizardError as exc:
        raise _wizard_api_error(exc) from exc
    return wizard.environment


@router.get("/wizard/session", response_model=WizardSessionOut | None)
async def wizard_session_state(
    principal: Annotated[Principal, Depends(require("connections"))],
    environment: Entorno = "production",
) -> WizardSessionOut | None:
    """En qué paso quedó el alta con Facebook de este negocio (sin tocar nada): la viva, o la
    última terminada. `null` si nunca empezó una."""
    from core.gateway.meta_onboarding import latest_wizard

    view = await latest_wizard(_negocio(principal), "whatsapp", environment)
    return await _view_out(view) if view is not None else None


class WizardSessionIn(BaseModel):
    environment: Entorno = "production"
    # Cierra el alta viva y empieza otra sobre la misma conexión (el borrador se reutiliza).
    restart: bool = False


@router.post("/wizard/session", response_model=WizardSessionOut, status_code=201)
async def wizard_session(
    payload: WizardSessionIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardSessionOut:
    """Arranca o reanuda el alta con Facebook. Un alta vencida (más de 24 h) se cierra y se
    empieza otra reutilizando el borrador. Si el alta ya pasó el popup, sigue con los pasos
    automáticos; si lo espera, devuelve un `attempt` nuevo para el canje."""
    from core.gateway.meta_onboarding import (
        PRE_EXCHANGE_STEPS,
        WizardError,
        complete_signup,
        issue_attempt,
        start_or_resume_wizard,
    )

    tenant_id = _negocio(principal)
    _guard_production(principal, payload.environment)
    try:
        wizard = await start_or_resume_wizard(
            tenant_id, "whatsapp", environment=payload.environment, admin_id=principal.admin.id,
            restart=payload.restart,
        )
        attempt = None
        if wizard.current_step in PRE_EXCHANGE_STEPS:
            attempt = await issue_attempt(wizard.id, principal.admin.id)
        elif wizard.current_step not in ("done", "register_number"):
            wizard = await complete_signup(wizard.id)
    except WizardError as exc:
        raise _wizard_api_error(exc) from exc
    return await _view_out(wizard, attempt=attempt)


class WizardCancelIn(BaseModel):
    """Evento `CANCEL` del popup (o el popup cerrado sin `code`). Los textos vienen de Meta a
    través del navegador: se guardan recortados, como dato."""

    wizard_id: uuid.UUID
    current_step: str | None = Field(default=None, max_length=200)
    error_message: str | None = Field(default=None, max_length=1000)
    error_code: str | None = Field(default=None, max_length=100)
    error_id: str | None = Field(default=None, max_length=200)
    session_id: str | None = Field(default=None, max_length=200)


@router.post("/wizard/cancel", response_model=WizardSessionOut)
async def wizard_cancel(
    payload: WizardCancelIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardSessionOut:
    """Registra que el popup se cerró o falló. El alta sigue viva en el mismo paso: el panel
    ofrece reintentar (un `POST /wizard/session` da un intento nuevo)."""
    from core.gateway.meta_onboarding import WizardError, record_cancel_or_progress

    _negocio(principal)
    # Cancelar anula el intento del popup: en producción, solo quien puede abrirlo.
    _guard_production(principal, await _wizard_env(payload.wizard_id))
    try:
        view = await record_cancel_or_progress(
            payload.wizard_id,
            current_step=payload.current_step,
            error_code=payload.error_code,
            error_message=payload.error_message,
            error_id=payload.error_id,
            session_id=payload.session_id,
        )
    except WizardError as exc:
        raise _wizard_api_error(exc) from exc
    log.info("embedded_signup_cancelado", wizard_id=str(payload.wizard_id), con_error=bool(payload.error_message))
    return await _view_out(view)


class WizardExchangeIn(BaseModel):
    """Lo que el panel recibe del popup: `code` (de `FB.login`, TTL de 30 s, se canjea solo en
    el servidor) y, del evento `WA_EMBEDDED_SIGNUP`, la WABA, el número y el portafolio.
    `attempt`: el de `POST /wizard/session`. `pin`: el PIN de 6 dígitos con el que se registra
    el número (el que ya tenga en verificación en dos pasos, o uno nuevo). `SecretStr`: no salen
    en el repr del modelo."""

    wizard_id: uuid.UUID
    code: SecretStr
    attempt: SecretStr
    event_type: str = Field(default="FINISH", max_length=64)
    waba_id: str = Field(default="", max_length=64)
    phone_number_id: str = Field(default="", max_length=64)
    business_id: str = Field(default="", max_length=64)
    pin: SecretStr | None = None


_ID_NUMERICO = re.compile(r"\d{1,32}")


def _graph_id(valor: str, campo: str) -> str | None:
    valor = valor.strip()
    if not valor:
        return None
    # Los IDs del popup (WABA, número, portafolio) son numéricos en Graph.
    if not _ID_NUMERICO.fullmatch(valor):
        # Van en la ruta de Graph: sin `/` ni `?` que la cambien.
        raise ApiError.bad_request(f"{campo} no tiene un formato válido.", "invalid_id")
    return valor


@router.post("/wizard/exchange", response_model=WizardSessionOut)
async def wizard_exchange(
    payload: WizardExchangeIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardSessionOut:
    """Canjea el código del Embedded Signup y encadena el alta: suscribir la WABA, registrar el
    número con el PIN, sincronizar plantillas y verificar salud (HU-SH-05).

    La WABA y el número del popup se guardan recién después del canje, verificados con el
    token nuevo y sin dueño en otro negocio (ADR-0095). Si el canje falla, 400 (hay que repetir
    el popup); si falla un paso posterior, 200 con el paso y el error para retomarlo."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_onboarding import (
        WizardError,
        complete_signup,
        consume_attempt,
        exchange_code,
    )

    _negocio(principal)
    _guard_production(principal, await _wizard_env(payload.wizard_id))
    codigo = payload.code.get_secret_value().strip()
    if not codigo or len(codigo) > 2048:
        raise ApiError.bad_request("Falta el código de Meta.", "missing_code")
    waba_id = _graph_id(payload.waba_id, "WABA ID")
    phone_number_id = _graph_id(payload.phone_number_id, "Phone Number ID")
    business_id = _graph_id(payload.business_id, "Business ID")
    pin = payload.pin.get_secret_value().strip() if payload.pin is not None else None
    try:
        view = await consume_attempt(
            payload.wizard_id,
            payload.event_type,
            attempt=payload.attempt.get_secret_value(),
            admin_id=principal.admin.id,
        )
        view = await exchange_code(
            view.id,
            codigo,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
            business_id=business_id,
            event_type=payload.event_type,
        )
    except WizardError as exc:
        raise _wizard_api_error(exc) from exc
    except GraphError as exc:
        # Meta rechazó el canje (code inválido o vencido): hay que repetir el popup.
        log.warning("embedded_signup_canje_rechazado", wizard_id=str(payload.wizard_id), code=exc.code)
        raise ApiError.bad_request(
            "Meta no aceptó el código del popup (vence a los 30 segundos): vuelve a conectar con Facebook.",
            "meta_error",
        ) from exc
    try:
        view = await complete_signup(view.id, pin=pin or None)
    except WizardError as exc:
        raise _wizard_api_error(exc) from exc
    return await _view_out(view)


class WizardStepIn(BaseModel):
    wizard_id: uuid.UUID
    # PIN de 6 dígitos del número (verificación en dos pasos). `SecretStr`: no sale en el repr.
    pin: SecretStr | None = None


@router.post("/wizard/resume", response_model=WizardSessionOut)
async def wizard_resume(
    payload: WizardStepIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardSessionOut:
    """Reintenta el alta desde el paso donde quedó (después del popup)."""
    from core.gateway.meta_onboarding import (
        PRE_EXCHANGE_STEPS,
        WizardError,
        complete_signup,
        get_wizard,
    )

    _negocio(principal)
    _guard_production(principal, await _wizard_env(payload.wizard_id))
    try:
        wizard = await get_wizard(payload.wizard_id)
        if wizard.current_step in PRE_EXCHANGE_STEPS:
            raise WizardError("Falta conectar con Facebook.", code="needs_popup", status=409)
        pin = payload.pin.get_secret_value().strip() if payload.pin is not None else None
        view = await complete_signup(payload.wizard_id, pin=pin or None)
    except WizardError as exc:
        raise _wizard_api_error(exc) from exc
    return await _view_out(view)


@router.post("/wizard/number", response_model=WizardSessionOut)
async def wizard_number(
    payload: WizardStepIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardSessionOut:
    """Paso «registrar el número» con el PIN del negocio, y lo que sigue."""
    from core.gateway.meta_onboarding import WizardError, complete_signup, register_number_step

    _negocio(principal)
    _guard_production(principal, await _wizard_env(payload.wizard_id))
    pin = payload.pin.get_secret_value().strip() if payload.pin is not None else None
    try:
        await register_number_step(payload.wizard_id, pin or None)
        view = await complete_signup(payload.wizard_id)
    except WizardError as exc:
        raise _wizard_api_error(exc) from exc
    return await _view_out(view)


@router.post("/wizard/pin", response_model=WizardSessionOut)
async def wizard_pin(
    payload: WizardStepIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardSessionOut:
    """Fija un PIN nuevo de verificación en dos pasos (el número tenía otro que nadie recuerda),
    registra el número con él y sigue con el alta."""
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_onboarding import WizardError, complete_signup, set_new_pin_step

    _negocio(principal)
    _guard_production(principal, await _wizard_env(payload.wizard_id))
    pin = payload.pin.get_secret_value().strip() if payload.pin is not None else None
    try:
        await set_new_pin_step(payload.wizard_id, pin or None)
        view = await complete_signup(payload.wizard_id)
    except WizardError as exc:
        raise _wizard_api_error(exc) from exc
    except GraphError as exc:
        raise ApiError.bad_request(exc.message[:200], "meta_error") from exc
    return await _view_out(view)


_ID_DE_GRAPH = re.compile(r"[A-Za-z0-9_-]{1,64}")


class WizardByokIn(BaseModel):
    app_id: str
    app_secret: str
    phone_number_id: str
    access_token: str
    waba_id: str
    verify_token: str = ""
    # "Prueba (mi app de Meta, número de prueba)" o "Producción (mi app de Meta)" (SPEC-08).
    # Sin el campo, prueba: es lo que hacía el wizard antes de HU-CONN-06.
    environment: Literal["sandbox", "production"] = "sandbox"
    # PIN de verificación en dos pasos (6 dígitos) para registrar el número si Meta no lo
    # reporta registrado. Opcional: sin él, la conexión queda `pending` y el registro se hace
    # después. `SecretStr`: no sale en el repr del modelo.
    pin: SecretStr | None = None


class WizardByokOut(BaseModel):
    """Resultado del alta BYOK. `needsRegistration`: la conexión quedó `pending` esperando
    que el número se registre en la Cloud API con su PIN (`registrationError` dice por qué
    no se pudo con el PIN enviado, si vino uno)."""

    # camelCase, como el resto de las respuestas del wizard (`connectionId`, `nextStep`).
    connectionId: str
    environment: str
    status: str
    phoneNumberId: str
    needsRegistration: bool = False
    registrationError: str | None = None


@router.post("/wizard/byok", response_model=WizardByokOut, status_code=201)
async def wizard_byok(
    payload: WizardByokIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardByokOut:
    """Alta con app propia (BYOK), de prueba o de producción: guarda credenciales, suscribe la
    WABA y deja la conexión. Si el número no está registrado en la Cloud API, lo registra con
    el PIN enviado; sin PIN (o si Meta lo rechaza), la conexión queda `pending` hasta que se
    registre (HU-CONN-06)."""
    from core.admin.meta_numbers_service import register_number
    from core.gateway.meta_byok import ByokError, save_byok_connection
    from core.gateway.meta_connection_state import esperando_registro

    campos = (payload.app_id, payload.app_secret, payload.phone_number_id, payload.access_token, payload.waba_id)
    if not all(c.strip() for c in campos):
        raise ApiError.bad_request("Faltan campos obligatorios de BYOK.", "missing_fields")
    if any(not _ID_DE_GRAPH.fullmatch(c.strip()) for c in (payload.app_id, payload.phone_number_id, payload.waba_id)):
        # Van en la ruta de Graph: sin `/` ni `?` que la cambien.
        raise ApiError.bad_request("App ID, WABA ID y Phone Number ID no tienen un formato válido.", "invalid_id")
    if payload.environment == "production":
        # Una conexión de producción atiende clientes reales: como "Pasar a producción" y
        # registrar el webhook, solo el propietario del negocio o el master (ADR-0127).
        require_owner_or_master(principal)
    phone_number_id = payload.phone_number_id.strip()
    try:
        connection = await save_byok_connection(
            principal.tenant_id,
            principal.admin.id,
            payload.app_id.strip(),
            payload.app_secret.strip(),
            phone_number_id,
            payload.access_token.strip(),
            payload.waba_id.strip(),
            environment=payload.environment,
            verify_token=payload.verify_token.strip() or None,
        )
    except ByokError as exc:
        raise ApiError.bad_request(str(exc)[:200], "byok_error") from exc

    status = connection.status
    registration_error: str | None = None
    pin = payload.pin.get_secret_value().strip() if payload.pin is not None else ""
    if esperando_registro(connection) and pin:
        registration_error = await register_number(
            principal.tenant_id, str(connection.id), phone_number_id, pin=pin
        )
        # El estado lo decide el registro (`_conectar_si_solo_faltaba_el_registro`): se relee.
        from core.db.models import MetaConnection

        async with get_session() as session:
            fila = await session.get(MetaConnection, connection.id)
            if fila is not None and fila.tenant_id == principal.tenant_id:
                status = fila.status
    return WizardByokOut(
        connectionId=str(connection.id),
        environment=connection.environment,
        status=status,
        phoneNumberId=phone_number_id,
        needsRegistration=status == "pending",
        registrationError=registration_error,
    )


class ManualConnectionIn(BaseModel):
    waba_id: str
    phone_number_id: str
    token: str
    business_id: str = ""


@router.post("/manual", status_code=201)
async def manual_connection(
    payload: ManualConnectionIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> dict[str, Any]:
    """Carga manual de respaldo: valida el token y crea conexión + WABA + número."""
    from core.gateway import meta_token_store
    from core.gateway.meta_asset_claims import MetaAssetError, verify_waba_and_phone
    from core.gateway.meta_health import check_token_valid
    from core.gateway.meta_onboarding import TOKEN_KIND

    waba_id = payload.waba_id.strip()
    numero = payload.phone_number_id.strip()
    token = payload.token.strip()
    if not (waba_id and numero and token):
        raise ApiError.bad_request("waba_id, phone_number_id y token son obligatorios.", "missing_fields")

    # Una conexión `manual` se resuelve SIEMPRE con la app de la plataforma (ADR-0094,
    # enmienda la corrección de ADR-0034): se valida con esa misma app, para que un token
    # que pasa la validación no falle después por `appsecret_proof`. Un negocio con su
    # propia app de Meta conecta por el wizard BYOK.
    settings = get_settings()
    if not settings.meta_app_id or not settings.meta_app_secret:
        raise ApiError.bad_request(
            "La app de Meta de la plataforma no está configurada.", "platform_app_not_configured"
        )
    app_id = settings.meta_app_id

    check = await check_token_valid(token, app_id, settings.meta_app_secret, settings.graph_api_version)
    if check.ok is False:
        raise ApiError.bad_request(check.detail[:200], "invalid_token")

    # La WABA y el número se verifican con ese token antes de guardarlos, y no pueden ser
    # de otro negocio (ADR-0095): el webhook rutea por ellos.
    try:
        await verify_waba_and_phone(
            waba_id=waba_id, phone_number_id=numero, token=token,
            app_secret=settings.meta_app_secret, version=settings.graph_api_version, connection_id=None,
        )
        connection_id = await _create_manual_connection(principal, payload, app_id, waba_id, numero)
    except MetaAssetError as exc:
        raise ApiError.bad_request(exc.message[:200], exc.code) from exc

    await meta_token_store.put_token(
        connection_id, TOKEN_KIND, payload.business_id.strip() or str(connection_id), token,
        tenant_id=principal.tenant_id,
    )
    return {"connectionId": str(connection_id)}


async def _create_manual_connection(
    principal: Principal, payload: ManualConnectionIn, app_id: str, waba_id: str, numero: str
) -> uuid.UUID:
    from core.db.models import MetaConnection, utcnow
    from core.gateway.meta_asset_claims import claim_assets

    async with get_session() as session:
        await claim_assets(session, principal.tenant_id, waba_id=waba_id, phone_number_id=numero)
        connection = MetaConnection(
            tenant_id=principal.tenant_id,
            channel="whatsapp",
            origin="manual",
            meta_app_id=app_id,
            business_id=payload.business_id.strip() or None,
            status="connected",
            connected_by=principal.admin.id,
            connected_at=utcnow(),
        )
        session.add(connection)
        await session.flush()
        # Revive las filas si el negocio ya tuvo este número o esta WABA (ADR-0122).
        from core.gateway.meta_asset_claims import numero_para_conexion, waba_para_conexion

        waba = await waba_para_conexion(session, principal.tenant_id, connection.id, waba_id)
        await numero_para_conexion(session, principal.tenant_id, connection.id, numero, waba_account_id=waba.id)
        return connection.id
