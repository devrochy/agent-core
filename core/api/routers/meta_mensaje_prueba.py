"""Mensaje de prueba del hub de una conexión de WhatsApp (HU-SH-05).

`GET` dice si se puede enviar (número, plantillas aprobadas sin variables y por qué no) sin
llamar a Meta; `POST` envía la plantilla al número que escribe el admin, con las credenciales
de la conexión. La lógica vive en `core.admin.meta_mensaje_prueba`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.admin.meta_mensaje_prueba import (
    MensajePruebaError,
    entorno_de,
    enviar_mensaje_de_prueba,
    opciones_de_prueba,
)
from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError

router = APIRouter(prefix="/admin/meta", tags=["admin · meta"])


class TestTemplate(BaseModel):
    name: str
    language: str


class TestMessageOptions(BaseModel):
    connection_id: uuid.UUID
    status: str
    phone_number_id: str | None
    display_phone_number: str | None
    templates: list[TestTemplate]
    can_send: bool
    blocked_reason: str | None


class TestMessageIn(BaseModel):
    to: str = Field(min_length=8, max_length=24)
    template_name: str | None = Field(default=None, max_length=512)
    language: str | None = Field(default=None, max_length=16)


class TestMessageOut(BaseModel):
    message_id: str
    template: TestTemplate


def _negocio(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Elige un negocio.", "tenant_required")
    return principal.tenant_id


def _cid(connection_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(connection_id)
    except ValueError as exc:
        raise ApiError.not_found("Conexión no encontrada") from exc


def _error(exc: MensajePruebaError) -> ApiError:
    if exc.status == 404:
        return ApiError.not_found(exc.message)
    if exc.status in (409, 429):
        return ApiError(exc.status, exc.code, exc.message)
    return ApiError.bad_request(exc.message, exc.code)


@router.get("/connections/{connection_id}/test-message", response_model=TestMessageOptions)
async def test_message_options(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> TestMessageOptions:
    try:
        opciones = await opciones_de_prueba(_negocio(principal), _cid(connection_id))
    except MensajePruebaError as exc:
        raise _error(exc) from exc
    return TestMessageOptions(
        connection_id=opciones.connection_id,
        status=opciones.status,
        phone_number_id=opciones.phone_number_id,
        display_phone_number=opciones.display_phone_number,
        templates=[TestTemplate(name=t.name, language=t.language) for t in opciones.templates],
        can_send=opciones.blocked_reason is None,
        blocked_reason=opciones.blocked_reason,
    )


@router.post("/connections/{connection_id}/test-message", response_model=TestMessageOut)
async def send_test_message(
    connection_id: str,
    payload: TestMessageIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> TestMessageOut:
    """Envía una plantilla aprobada sin variables al número indicado (uno cada 30 s, 10 al día).
    En producción, solo el propietario del negocio o el master."""
    tenant_id, cid = _negocio(principal), _cid(connection_id)
    try:
        if await entorno_de(tenant_id, cid) == "production":
            require_owner_or_master(principal)
        message_id, plantilla = await enviar_mensaje_de_prueba(
            tenant_id,
            cid,
            payload.to,
            template_name=(payload.template_name or "").strip() or None,
            language=(payload.language or "").strip() or None,
            admin_id=principal.admin.id,
        )
    except MensajePruebaError as exc:
        raise _error(exc) from exc
    return TestMessageOut(message_id=message_id, template=TestTemplate(name=plantilla.name, language=plantilla.language))
