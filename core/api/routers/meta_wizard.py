"""Wizard de alta de WhatsApp en la API (ADR-0080).

Reutiliza los mismos servicios que el panel clásico: `core.gateway.meta_onboarding`
(estado del wizard, exchange del Embedded Signup, registro de número y PIN),
`core.gateway.meta_byok` (alta con app propia) y la carga manual como escape.
Las credenciales de Meta (App ID/Secret/Config ID) se leen del entorno; sin ellas
las pantallas y los errores siguen funcionando, pero el flujo real no se completa.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.config import get_settings
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/admin/meta", tags=["admin · wizard whatsapp"])


class WizardState(BaseModel):
    meta_configured: bool
    app_id: str
    config_id: str
    environments: list[str]


@router.get("/wizard", response_model=WizardState)
async def wizard_state(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardState:
    """Qué puede ofrecer el wizard en este entorno (app de Meta configurada o no)."""
    settings = get_settings()
    return WizardState(
        meta_configured=bool(settings.meta_app_id and settings.meta_config_id),
        app_id=settings.meta_app_id or "",
        config_id=settings.meta_config_id or "",
        environments=["sandbox", "production"],
    )


class WizardSessionIn(BaseModel):
    environment: str = "production"


class WizardSessionOut(BaseModel):
    wizard_id: uuid.UUID
    step: str
    connection_id: uuid.UUID | None = None


def _view_out(view: Any) -> WizardSessionOut:
    return WizardSessionOut(
        wizard_id=view.id,
        step=view.current_step,
        connection_id=view.connection_id,
    )


@router.post("/wizard/session", response_model=WizardSessionOut, status_code=201)
async def wizard_session(
    payload: WizardSessionIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardSessionOut:
    """Arranca (o reanuda) la sesión del wizard para el negocio."""
    from core.gateway.meta_onboarding import (
        WizardError,
        run_automatic_steps,
        start_or_resume_wizard,
    )

    entorno = payload.environment if payload.environment in ("sandbox", "production") else "production"
    try:
        wizard = await start_or_resume_wizard(
            principal.tenant_id, "whatsapp", environment=entorno, admin_id=principal.admin.id
        )
        wizard = await run_automatic_steps(wizard.id)
    except WizardError as exc:
        raise ApiError.bad_request(str(exc)[:200], "wizard_error") from exc
    return _view_out(wizard)


class WizardExchangeIn(BaseModel):
    wizard_id: uuid.UUID
    code: str
    event_type: str = "FINISH"
    waba_id: str = ""
    phone_number_id: str = ""
    business_id: str = ""


@router.post("/wizard/exchange")
async def wizard_exchange(
    payload: WizardExchangeIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> dict[str, Any]:
    """Canjea el código del Embedded Signup (el popup ya pudo dar waba/phone)."""
    from core.gateway.meta_onboarding import WizardError, exchange_code, persist_finish_payload

    codigo = payload.code.strip()
    if not codigo:
        raise ApiError.bad_request("Falta el código de Meta.", "missing_code")
    try:
        view = await persist_finish_payload(
            payload.wizard_id,
            payload.event_type,
            waba_id=payload.waba_id.strip() or None,
            phone_number_id=payload.phone_number_id.strip() or None,
            business_id=payload.business_id.strip() or None,
        )
        view = await exchange_code(view.id, codigo)
    except WizardError as exc:
        raise ApiError.bad_request(str(exc)[:200], "wizard_error") from exc
    return {
        "connectionId": str(view.connection_id) if view.connection_id else None,
        "nextStep": view.current_step,
    }


class WizardStepIn(BaseModel):
    wizard_id: uuid.UUID


@router.post("/wizard/number", response_model=WizardSessionOut)
async def wizard_number(
    payload: WizardStepIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardSessionOut:
    """Paso «registrar el número» del wizard."""
    from core.gateway.meta_onboarding import WizardError, register_number_step, run_automatic_steps

    try:
        view = await register_number_step(payload.wizard_id)
        view = await run_automatic_steps(view.id)
    except WizardError as exc:
        raise ApiError.bad_request(str(exc)[:200], "wizard_error") from exc
    return _view_out(view)


@router.post("/wizard/pin", response_model=WizardSessionOut)
async def wizard_pin(
    payload: WizardStepIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> WizardSessionOut:
    """Paso «fijar PIN nuevo» (verificación en dos pasos)."""
    from core.gateway.meta_onboarding import WizardError, run_automatic_steps, set_new_pin_step

    try:
        view = await set_new_pin_step(payload.wizard_id)
        view = await run_automatic_steps(view.id)
    except WizardError as exc:
        raise ApiError.bad_request(str(exc)[:200], "wizard_error") from exc
    return _view_out(view)


class WizardByokIn(BaseModel):
    app_id: str
    app_secret: str
    phone_number_id: str
    access_token: str
    waba_id: str
    verify_token: str = ""


@router.post("/wizard/byok", status_code=201)
async def wizard_byok(
    payload: WizardByokIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> dict[str, Any]:
    """Alta con app propia (BYOK): guarda credenciales, suscribe la WABA y deja la conexión."""
    from core.gateway.meta_byok import ByokError, save_byok_connection

    campos = (payload.app_id, payload.app_secret, payload.phone_number_id, payload.access_token, payload.waba_id)
    if not all(c.strip() for c in campos):
        raise ApiError.bad_request("Faltan campos obligatorios de BYOK.", "missing_fields")
    try:
        connection = await save_byok_connection(
            principal.tenant_id,
            principal.admin.id,
            payload.app_id.strip(),
            payload.app_secret.strip(),
            payload.phone_number_id.strip(),
            payload.access_token.strip(),
            payload.waba_id.strip(),
            verify_token=payload.verify_token.strip() or None,
        )
    except ByokError as exc:
        raise ApiError.bad_request(str(exc)[:200], "byok_error") from exc
    return {"connectionId": str(getattr(connection, "id", connection))}


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
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount, utcnow
    from core.gateway import meta_token_store
    from core.gateway.meta_health import check_token_valid
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.gateway.meta_preflight import resolve_app_credentials

    waba_id = payload.waba_id.strip()
    numero = payload.phone_number_id.strip()
    token = payload.token.strip()
    if not (waba_id and numero and token):
        raise ApiError.bad_request("waba_id, phone_number_id y token son obligatorios.", "missing_fields")

    settings = get_settings()
    creds = await resolve_app_credentials(principal.tenant_id, "whatsapp")
    app_id = creds.app_id if creds else settings.meta_app_id
    app_secret = creds.app_secret if creds else settings.meta_app_secret

    check = await check_token_valid(token, app_id, app_secret, settings.graph_api_version)
    if check.ok is False:
        raise ApiError.bad_request(check.detail[:200], "invalid_token")

    async with get_session() as session:
        connection = MetaConnection(
            tenant_id=principal.tenant_id,
            channel="whatsapp",
            meta_app_id=app_id,
            business_id=payload.business_id.strip() or None,
            status="connected",
            connected_by=principal.admin.id,
            connected_at=utcnow(),
        )
        session.add(connection)
        await session.flush()
        waba = WabaAccount(
            tenant_id=principal.tenant_id, connection_id=connection.id, waba_id=waba_id
        )
        session.add(waba)
        await session.flush()
        session.add(
            PhoneNumber(
                tenant_id=principal.tenant_id,
                connection_id=connection.id,
                waba_account_id=waba.id,
                phone_number_id=numero,
            )
        )
        connection_id = connection.id

    await meta_token_store.put_token(
        connection_id, TOKEN_KIND, payload.business_id.strip() or str(connection_id), token,
        tenant_id=principal.tenant_id,
    )
    return {"connectionId": str(connection_id)}
