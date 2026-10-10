"""Plataforma → App de Meta: requisitos de Embedded Signup (solo master, HU-SH-05).

`GET /api/v1/admin/platform/meta-app/embedded-signup` y `POST .../embedded-signup/step`. La
lógica vive en `core.admin.platform_meta_es`. Los chequeos del servidor (variables de la app,
webhook en HTTPS, suscripción, secretos, RLS, worker) son de HU-CONN-07
(`/admin/platform/meta-app`): aquí solo se resume su última corrida en `platform_check`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from core.admin.platform_meta_es import RequisitoError, estado_app_meta, marcar_requisito
from core.api.deps import Principal, get_principal
from core.api.errors import ApiError

router = APIRouter(prefix="/admin/platform/meta-app", tags=["admin · plataforma"])


class EsPlatformCheck(BaseModel):
    """Última revisión de "Plataforma → App de Meta" (HU-CONN-07). `ok`: terminó, sin ítems en
    rojo y con las variables de la app, el webhook y la suscripción en verde. `pending`: las
    etiquetas de los ítems que lo impiden."""

    status: Literal["never", "queued", "running", "done", "failed"]
    finished_at: datetime | None
    ok: bool
    pending: list[str]


class EsManualStep(BaseModel):
    key: str
    label: str
    help: str
    link: str
    done: bool
    notes: str | None
    updated_at: datetime | None


class EmbeddedSignupRequirements(BaseModel):
    """`ready`: la revisión de la plataforma en verde (`platform_check.ok`) y todo lo manual
    confirmado."""

    app_id: str
    config_id: str
    graph_api_version: str
    ready: bool
    platform_check: EsPlatformCheck
    manual: list[EsManualStep]


class EsStepIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    done: bool
    notes: str | None = Field(default=None, max_length=500)


def _master(principal: Principal) -> str:
    if principal.admin.role != "master":
        raise ApiError.forbidden("Solo el admin de plataforma puede ver esto.")
    return principal.admin.role


def _error(exc: RequisitoError) -> ApiError:
    if exc.status == 403:
        return ApiError.forbidden(exc.message)
    if exc.status == 404:
        return ApiError.not_found(exc.message)
    return ApiError.bad_request(exc.message, exc.code)


@router.get("/embedded-signup", response_model=EmbeddedSignupRequirements)
async def embedded_signup_requirements(
    principal: Annotated[Principal, Depends(get_principal)],
) -> EmbeddedSignupRequirements:
    try:
        _master(principal)
        estado = await estado_app_meta(principal.admin)
    except RequisitoError as exc:
        raise _error(exc) from exc
    return EmbeddedSignupRequirements(
        app_id=estado.app_id,
        config_id=estado.config_id,
        graph_api_version=estado.graph_api_version,
        ready=estado.ready,
        platform_check=EsPlatformCheck(**estado.platform_check.__dict__),
        manual=[EsManualStep(**m.__dict__) for m in estado.manual],
    )


@router.post("/embedded-signup/step", status_code=204)
async def embedded_signup_step(
    payload: EsStepIn,
    principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    """Marca o desmarca un requisito manual (con nota), guardando quién y cuándo."""
    try:
        await marcar_requisito(
            _master(principal), principal.admin.id, payload.key.strip(), done=payload.done, notes=payload.notes
        )
    except RequisitoError as exc:
        raise _error(exc) from exc
    return Response(status_code=204)
