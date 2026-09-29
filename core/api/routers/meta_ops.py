"""Salud de conexiones Meta y suscripciones de webhook.

`GET /api/v1/admin/meta/health` y `/webhooks` (lectura) más las acciones de
reparación: diagnosticar, re-suscribir la WABA, marcar facturación y registrar la
suscripción de APP de un canal. Reusa `core.admin.meta_health_service` (fuente
única con el panel SSR). Requiere la sección `connections`; registrar exige
además ser `owner` del negocio o `master` (igual que el SSR).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select

from core.admin.meta_health_service import (
    diagnose_connection,
    health_overview,
    register_channel_subscription,
    resubscribe_waba,
    set_billing_status,
    set_connection_status,
    soft_delete_connection,
    webhook_subscriptions_status,
)
from core.admin.meta_numbers_service import (
    add_number,
    list_numbers,
    request_code,
    verify_number,
)
from core.admin.meta_templates_service import (
    delete_template,
    list_templates,
    sync_templates_now,
)
from core.admin.waweb_service import (
    accept_risk as waweb_accept_risk,
)
from core.admin.waweb_service import (
    reset_session as waweb_reset_session,
)
from core.admin.waweb_service import (
    session_state as waweb_session_state,
)
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.config import get_settings
from core.db.session import get_session

router = APIRouter(prefix="/admin/meta", tags=["admin · meta"])


class HealthConnection(BaseModel):
    id: uuid.UUID
    channel: str
    environment: str
    status: str
    status_reason: str | None
    last_health_check_at: datetime | None
    health: dict[str, Any]
    business_id: str | None
    meta_app_id: str
    token_expires_at: datetime | None = None


class HealthResponse(BaseModel):
    connections: list[HealthConnection]


class BillingRequest(BaseModel):
    manual_status: str


class SubscriptionItem(BaseModel):
    channel: str
    object: str
    meta_app_id: str
    callback_url: str | None
    active: bool | None
    fields: list[str]
    last_error: str | None


class WebhooksResponse(BaseModel):
    subscriptions: list[SubscriptionItem]


class RegisterRequest(BaseModel):
    channel: str


def _require_owner_or_master(principal: Principal) -> None:
    if principal.admin.role == "master":
        return
    role = next((role for (tid, _name, role) in principal.tenants if tid == principal.tenant_id), None)
    if role != "owner":
        raise ApiError.forbidden(
            "Se requiere ser propietario del negocio o admin de plataforma."
        )


@router.get("/health", response_model=HealthResponse)
async def get_health(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> HealthResponse:
    rows = await health_overview(principal.tenant_id)
    return HealthResponse(connections=[HealthConnection(**row) for row in rows])


@router.post("/health/{connection_id}/diagnose", status_code=204)
async def diagnose(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await diagnose_connection(principal.tenant_id, connection_id)
    if error is not None:
        raise ApiError.bad_request(error, "diagnose_failed")
    return Response(status_code=204)


@router.post("/health/{connection_id}/resubscribe", status_code=204)
async def resubscribe(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await resubscribe_waba(principal.tenant_id, connection_id)
    if error is not None:
        raise ApiError.bad_request(error, "resubscribe_failed")
    return Response(status_code=204)


@router.post("/health/{connection_id}/billing", status_code=204)
async def billing(
    connection_id: str,
    payload: BillingRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await set_billing_status(
        principal.tenant_id, connection_id, payload.manual_status
    )
    if error is not None:
        raise ApiError.bad_request(error, "billing_failed")
    return Response(status_code=204)


@router.get("/webhooks", response_model=WebhooksResponse)
async def get_webhooks(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> WebhooksResponse:
    rows = await webhook_subscriptions_status(principal.tenant_id)
    return WebhooksResponse(subscriptions=[SubscriptionItem(**row) for row in rows])


@router.post("/webhooks/register", status_code=204)
async def register_webhook(
    payload: RegisterRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    _require_owner_or_master(principal)
    error = await register_channel_subscription(principal.tenant_id, payload.channel)
    if error is not None:
        raise ApiError.bad_request(error, "register_failed")
    return Response(status_code=204)


# ------------------------------------------------------------------ números
class PhoneNumberItem(BaseModel):
    id: uuid.UUID
    phone_number_id: str
    display_phone_number: str | None
    verified_name: str | None
    status: str


class NumbersResponse(BaseModel):
    numbers: list[PhoneNumberItem]
    sync_error: str | None = None


class AddNumberRequest(BaseModel):
    cc: str
    phone_number: str
    verified_name: str


class RequestCodeRequest(BaseModel):
    code_method: str
    language: str = "es"


class VerifyCodeRequest(BaseModel):
    code: str


@router.get("/connections/{connection_id}/numbers", response_model=NumbersResponse)
async def list_connection_numbers(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> NumbersResponse:
    """Números de una conexión Meta (sincroniza con Meta antes de listar)."""
    rows, sync_error = await list_numbers(principal.tenant_id, connection_id)
    return NumbersResponse(
        numbers=[PhoneNumberItem(**row) for row in rows], sync_error=sync_error
    )


@router.post("/connections/{connection_id}/numbers", status_code=204)
async def add_connection_number(
    connection_id: str,
    payload: AddNumberRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await add_number(
        principal.tenant_id,
        connection_id,
        cc=payload.cc,
        phone_number=payload.phone_number,
        verified_name=payload.verified_name,
    )
    if error is not None:
        raise ApiError.bad_request(error, "add_number_failed")
    return Response(status_code=204)


@router.post("/connections/{connection_id}/numbers/{phone_number_id}/request-code", status_code=204)
async def request_connection_code(
    connection_id: str,
    phone_number_id: str,
    payload: RequestCodeRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await request_code(
        principal.tenant_id,
        connection_id,
        phone_number_id,
        code_method=payload.code_method,
        language=payload.language,
    )
    if error is not None:
        raise ApiError.bad_request(error, "request_code_failed")
    return Response(status_code=204)


@router.post("/connections/{connection_id}/numbers/{phone_number_id}/verify", status_code=204)
async def verify_connection_number(
    connection_id: str,
    phone_number_id: str,
    payload: VerifyCodeRequest,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    ok, error = await verify_number(
        principal.tenant_id, connection_id, phone_number_id, code=payload.code
    )
    if error is not None:
        raise ApiError.bad_request(error, "verify_failed")
    if not ok:
        raise ApiError.bad_request("Código incorrecto.", "wrong_code")
    return Response(status_code=204)


# ------------------------------------------------------------------ plantillas
class TemplateItem(BaseModel):
    id: uuid.UUID
    name: str
    language: str
    category: str | None
    status: str
    quality_score: str | None
    rejected_reason: str | None


class TemplatesResponse(BaseModel):
    templates: list[TemplateItem]
    sync_error: str | None = None
    at_limit: bool = False


@router.get("/connections/{connection_id}/templates", response_model=TemplatesResponse)
async def list_connection_templates(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> TemplatesResponse:
    """Plantillas de la WABA (sincroniza antes de listar, con lock de 1 h)."""
    rows, sync_error, at_limit = await list_templates(principal.tenant_id, connection_id)
    return TemplatesResponse(
        templates=[TemplateItem(**row) for row in rows],
        sync_error=sync_error,
        at_limit=at_limit,
    )


@router.post("/connections/{connection_id}/templates/sync", status_code=204)
async def sync_connection_templates(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await sync_templates_now(principal.tenant_id, connection_id)
    if error is not None:
        raise ApiError.bad_request(error, "sync_failed")
    return Response(status_code=204)


@router.post("/connections/{connection_id}/templates/{template_id}/delete", status_code=204)
async def delete_connection_template(
    connection_id: str,
    template_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Borra TODAS las versiones de idioma de la plantilla."""
    error = await delete_template(principal.tenant_id, connection_id, template_id)
    if error is not None:
        raise ApiError.bad_request(error, "delete_failed")
    return Response(status_code=204)


# ------------------------------------------------------------------ WhatsApp Web (ADR-0074)
class WawebState(BaseModel):
    status: str
    risk_ok: bool
    jid: str | None = None
    qr_image: str | None = None


@router.get("/waweb", response_model=WawebState)
async def get_waweb_state(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> WawebState:
    """Estado del vínculo por QR del canal WhatsApp Web (no oficial)."""
    state = await waweb_session_state(principal.tenant_id)
    return WawebState(**state)


@router.post("/waweb/accept-risk", status_code=204)
async def accept_waweb_risk(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await waweb_accept_risk(principal.tenant_id, principal.admin.id)
    if error is not None:
        raise ApiError.bad_request(error, "accept_risk_failed")
    return Response(status_code=204)


@router.post("/waweb/reset", status_code=204)
async def reset_waweb_session(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    error = await waweb_reset_session(principal.tenant_id)
    if error is not None:
        raise ApiError.bad_request(error, "reset_failed")
    return Response(status_code=204)


class MetaConnectionAction(BaseModel):
    action: Literal["activate", "deactivate", "delete"]


@router.post("/connections/{connection_id}/action", status_code=204)
async def meta_connection_action(
    connection_id: str,
    payload: MetaConnectionAction,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Activa, desactiva o elimina (soft-delete) una conexión de Meta."""
    if payload.action == "delete":
        error = await soft_delete_connection(principal.tenant_id, connection_id)
    else:
        error = await set_connection_status(
            principal.tenant_id, connection_id, active=payload.action == "activate"
        )
    if error is not None:
        raise ApiError.bad_request(error, "meta_connection_failed")
    return Response(status_code=204)

class TemplateCreate(BaseModel):
    """Alta de plantilla (texto; los encabezados con multimedia se suben aparte)."""

    name: str
    language: str = "es"
    category: str = "UTILITY"
    header_format: str = "NONE"
    header_text: str = ""
    header_param_name: str = ""
    header_param_example: str = ""
    body_text: str
    body_param_names: str = ""
    body_param_examples: str = ""
    footer_text: str = ""
    buttons_json: str = ""
    parameter_format: str = ""


@router.post("/connections/{connection_id}/templates", status_code=201)
async def create_connection_template(
    connection_id: str,
    payload: TemplateCreate,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Crea una plantilla en la WABA de la conexión (pendiente de revisión de Meta)."""
    import uuid as uuid_mod

    from core.admin.views import _build_template_components, _resolve_waba_and_token
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_template_upload import TemplateUploadError
    from core.gateway.meta_templates import (
        TemplateRateLimitError,
        TemplateValidationError,
        create_template,
    )

    if payload.header_format in ("IMAGE", "VIDEO", "DOCUMENT"):
        raise ApiError.bad_request(
            "Los encabezados con multimedia aún no están soportados (pendiente: subida de archivo).",
            "media_header_not_supported",
        )
    if not payload.name.strip() or not payload.body_text.strip():
        raise ApiError.bad_request("Nombre y cuerpo son obligatorios.", "missing_fields")
    try:
        connection, waba, token = await _resolve_waba_and_token(
            uuid_mod.UUID(connection_id), principal.tenant_id
        )
    except ValueError as exc:
        raise ApiError.bad_request("Conexión inválida.", "invalid_connection") from exc
    except Exception as exc:  # conexión inexistente / sin WABA / sin token
        raise ApiError.bad_request(
            f"No hay conexión/WABA lista todavía: {str(exc)[:120]}", "connection_not_ready"
        ) from exc
    if connection is None or waba is None or not token:
        raise ApiError.bad_request(
            "No hay conexión/WABA lista todavía.", "connection_not_ready"
        )

    form = {
        "name": payload.name.strip(),
        "language": payload.language.strip() or "es",
        "category": payload.category.strip() or "UTILITY",
        "header_format": payload.header_format,
        "header_text": payload.header_text,
        "header_param_name": payload.header_param_name,
        "header_param_example": payload.header_param_example,
        "body_text": payload.body_text,
        "body_param_names": payload.body_param_names,
        "body_param_examples": payload.body_param_examples,
        "footer_text": payload.footer_text,
        "buttons_json": payload.buttons_json,
        "parameter_format": payload.parameter_format,
    }
    components = _build_template_components(form)
    try:
        await create_template(
            waba.waba_id,
            token,
            get_settings().meta_app_secret,
            connection.graph_api_version,
            connection.id,
            principal.tenant_id,
            waba.id,
            name=form["name"],
            language=form["language"],
            category=form["category"],
            components=components,
            parameter_format=form.get("parameter_format") or None,
        )
    except (TemplateValidationError, TemplateRateLimitError, TemplateUploadError) as exc:
        raise ApiError.bad_request(str(exc)[:200], "template_rejected") from exc
    except GraphError as exc:
        raise ApiError.bad_request(str(getattr(exc, "message", exc))[:200], "graph_error") from exc
    return Response(status_code=201)

# ------------------------------------------------- checklist de puesta en marcha
class ChecklistStep(BaseModel):
    key: str
    label: str
    link: str
    done: bool = False
    notes: str = ""


class PreflightOut(BaseModel):
    key: str
    # `None` = no evaluable en este entorno (los checks de producción devuelven
    # False/None fuera de un despliegue real; se muestran tal cual).
    ok: bool | None = None
    detail: str | None = None


class ChecklistOut(BaseModel):
    steps: list[ChecklistStep]
    checks: list[PreflightOut] = []
    meta_configured: bool = False


class ChecklistStepIn(BaseModel):
    step_key: str
    done: bool = False
    notes: str = ""


async def _checklist_steps(tenant_id: Any) -> tuple[list[ChecklistStep], bool]:
    """Pasos manuales de Meta con su estado (checklist del tenant)."""

    from core.db.models import MetaSetupChecklist
    from core.gateway.meta_preflight import MANUAL_STEPS, resolve_app_credentials

    config = await resolve_app_credentials(tenant_id)
    base = (
        f"https://developers.facebook.com/apps/{config.app_id}/"
        if config
        else "https://developers.facebook.com/apps/"
    )
    async with get_session() as session:
        filas = (
            await session.execute(
                select(MetaSetupChecklist).where(MetaSetupChecklist.tenant_id == tenant_id)
            )
        ).scalars().all()
    estado = {f.step_key: f for f in filas}
    return (
        [
            ChecklistStep(
                key=paso["key"],
                label=paso["label"],
                link=base + paso.get("link_suffix", ""),
                done=bool(getattr(estado.get(paso["key"]), "done", False)),
                notes=getattr(estado.get(paso["key"]), "notes", "") or "",
            )
            for paso in MANUAL_STEPS
        ],
        config is not None,
    )


@router.get("/checklist/requirements", response_model=ChecklistOut)
async def checklist_requirements(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> ChecklistOut:
    """Requisitos de puesta en marcha + los checks automáticos de Meta."""
    from core.db.models import Tenant
    from core.gateway.meta_preflight import run_preflight

    steps, meta_configured = await _checklist_steps(principal.tenant_id)
    slug = ""
    async with get_session() as session:
        tenant = await session.get(Tenant, principal.tenant_id)
        slug = tenant.slug if tenant else ""
    checks = await run_preflight(principal.tenant_id, slug)
    return ChecklistOut(
        steps=steps,
        checks=[PreflightOut(key=c.key, ok=c.ok, detail=c.detail) for c in checks],
        meta_configured=meta_configured,
    )


@router.get("/checklist/production", response_model=ChecklistOut)
async def checklist_production(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> ChecklistOut:
    """Lista de producción: los mismos pasos manuales, con su estado."""
    steps, meta_configured = await _checklist_steps(principal.tenant_id)
    return ChecklistOut(steps=steps, meta_configured=meta_configured)


@router.post("/checklist/step", status_code=204)
async def checklist_step(
    payload: ChecklistStepIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> Response:
    """Marca o desmarca un paso del checklist (con nota opcional)."""

    from core.db.models import MetaSetupChecklist

    if not payload.step_key.strip():
        raise ApiError.bad_request("Falta el paso.", "missing_step")
    async with get_session() as session:
        fila = (
            await session.execute(
                select(MetaSetupChecklist).where(
                    MetaSetupChecklist.tenant_id == principal.tenant_id,
                    MetaSetupChecklist.step_key == payload.step_key,
                )
            )
        ).scalar_one_or_none()
        if fila is None:
            fila = MetaSetupChecklist(
                tenant_id=principal.tenant_id, step_key=payload.step_key
            )
            session.add(fila)
        fila.done = payload.done
        fila.notes = payload.notes.strip() or None
    return Response(status_code=204)

@router.post("/checklist/production/run", response_model=list[PreflightOut])
async def run_production_checks(
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> list[PreflightOut]:
    """Corre las comprobaciones de puesta en producción (9 checks on-demand)."""
    from core.gateway import meta_production_readiness as readiness

    settings = get_settings()
    callback_url = f"{settings.public_base_url.rstrip('/')}/webhooks/whatsapp"
    checks = [
        await readiness.check_webhook_handshake(callback_url, settings.whatsapp_verify_token),
        await readiness.check_app_subscriptions_complete(settings.meta_app_id),
        readiness.check_prod_secret_differs_from_dev(settings.meta_app_secret),
        readiness.check_token_keys_configured(),
        await readiness.check_worker_heartbeat_and_dlq(),
        await readiness.check_rls_forced(),
        readiness.check_rls_tests_pass(),
        readiness.check_no_secret_leak_in_templates(),
        await readiness.check_connections_health_except_billing(principal.tenant_id),
    ]
    return [PreflightOut(key=c.key, ok=c.ok, detail=c.detail) for c in checks]
