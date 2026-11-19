"""Pasar a producción una conexión BYOK de prueba (HU-CONN-06, ADR-0127).

El panel guía el cambio en cuatro pasos (número real, registro con PIN, plantillas y
verificar); la lógica vive en `core.gateway.meta_byok_promocion`. El registro con PIN y las
plantillas usan las rutas de siempre (`/connections/{id}/numbers/.../register` y
`/connections/{id}/templates`). Leer el estado pide la sección `connections`; cambiar el
número o el entorno, escribir en ella y ser propietario del negocio o master (como
registrar el webhook): se reemplazan el número y el token por los que atienden clientes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, SecretStr

from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError
from core.api.schemas import ReadinessItem
from core.gateway.meta_byok_promocion import (
    TOPE_CHEQUEOS_S,
    PromocionError,
    cargar_numero_real,
    estado_promocion,
    promotion_item_labels,
    terminar_promocion,
    verificar_promocion,
)
from core.gateway.meta_preflight import PreflightCheck

router = APIRouter(prefix="/admin/meta", tags=["admin · meta"])


class PromotionStep(BaseModel):
    key: str
    label: str
    done: bool
    detail: str = ""


class PromotionState(BaseModel):
    """Dónde va la promoción (con lo guardado, sin llamar a Meta). Los pasos 1 a 3 miran la
    conexión de producción de la misma app (`production_connection_id`), que crea el paso 1;
    la de prueba sigue igual hasta terminar. `blocked_reason`: por qué no se puede pasar a
    producción (no es BYOK, usa la app de la plataforma, está desactivada)."""

    connection_id: uuid.UUID
    environment: str
    status: str
    status_reason: str | None
    meta_app_id: str
    production_connection_id: uuid.UUID | None
    production_status: str | None
    production_status_reason: str | None
    phone_number_id: str | None
    blocked_reason: str | None
    steps: list[PromotionStep]


class PromotionChecks(BaseModel):
    """Los ítems del checklist por conexión (HU-CONN-07) más los de la promoción, con el mismo
    formato que "Producción" de la conexión."""

    checks: list[ReadinessItem]


class PromotionResult(BaseModel):
    promoted: bool
    checks: list[ReadinessItem]
    state: PromotionState


class PromotionNumberIn(BaseModel):
    """Número de producción. `access_token`: el token permanente (System User) con acceso a
    esa WABA; sin él se usa el guardado. `SecretStr`: no sale en el repr."""

    # IDs de Graph: solo dígitos, para que no armen otra ruta de Graph (`/`, `?`) ni esquiven
    # las comparaciones del reclamo de activos.
    waba_id: str = Field(pattern=r"^\s*\d{1,25}\s*$")
    phone_number_id: str = Field(pattern=r"^\s*\d{1,25}\s*$")
    access_token: SecretStr | None = None


def _error(exc: PromocionError) -> ApiError:
    if exc.status == 404:
        return ApiError.not_found(exc.message)
    if exc.status in (409, 504):
        return ApiError(exc.status, exc.code, exc.message[:200])
    return ApiError.bad_request(exc.message[:200], exc.code)


_ESPERA_CHEQUEOS_S = 10


@asynccontextmanager
async def _limitar_chequeos(tenant_id: uuid.UUID, cid: uuid.UUID) -> AsyncIterator[None]:
    """Un "Verificar"/"Terminar" a la vez por negocio y 10 s de espera después: cada uno son
    varias llamadas a Graph con el token del negocio (su cuota de Meta) y corren en el request.
    El candado es del negocio (no de la conexión pedida): pedir la de prueba y la de producción
    a la vez no corre dos revisiones de la misma pareja. Antes, 404 si la conexión no es de este
    negocio: el límite no dice nada de conexiones ajenas."""
    from core.db.session import get_session
    from core.gateway.meta_connection_state import get_live_meta_connection
    from core.memory.working import get_redis

    async with get_session() as session:
        if await get_live_meta_connection(session, cid, tenant_id) is None:
            raise ApiError.not_found("Conexión no encontrada")

    clave = f"promocion_chequeos:{tenant_id}"
    redis = get_redis()
    # Mientras corre, el candado dura más que el tope de los chequeos; al terminar quedan 10 s.
    if not await redis.set(clave, "1", nx=True, ex=TOPE_CHEQUEOS_S + _ESPERA_CHEQUEOS_S):
        raise ApiError(429, "too_many_checks", "Espera unos segundos antes de volver a verificar.")
    try:
        yield
    finally:
        await redis.expire(clave, _ESPERA_CHEQUEOS_S)


def _negocio(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Elige un negocio.", "tenant_required")
    return principal.tenant_id


def _connection_uuid(connection_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(connection_id)
    except ValueError as exc:
        raise ApiError.not_found("Conexión no encontrada") from exc


async def _state(tenant_id: uuid.UUID, cid: uuid.UUID) -> PromotionState:
    try:
        return PromotionState(**await estado_promocion(tenant_id, cid))
    except PromocionError as exc:
        raise _error(exc) from exc


def _checks_out(checks: list[PreflightCheck]) -> list[ReadinessItem]:
    from core.admin.production_readiness_service import serialize_checks

    return [ReadinessItem(**item) for item in serialize_checks(checks, promotion_item_labels())]


@router.get("/connections/{connection_id}/promotion", response_model=PromotionState)
async def promotion_state(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections"))],
) -> PromotionState:
    """Pasos de "Pasar a producción" de una conexión BYOK de prueba."""
    return await _state(_negocio(principal), _connection_uuid(connection_id))


@router.post("/connections/{connection_id}/promotion/number", response_model=PromotionState)
async def promotion_number(
    connection_id: str,
    payload: PromotionNumberIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> PromotionState:
    """Paso 1: crea (o actualiza) la conexión de producción de la misma app con el número real
    y su WABA, con el alta BYOK. Si el número no está registrado en la Cloud API, esa conexión
    queda `pending` hasta registrarlo con su PIN. La de prueba no cambia."""
    require_owner_or_master(principal)
    cid = _connection_uuid(connection_id)
    try:
        await cargar_numero_real(
            _negocio(principal),
            principal.admin.id,
            cid,
            waba_id=payload.waba_id,
            phone_number_id=payload.phone_number_id,
            access_token=payload.access_token.get_secret_value() if payload.access_token is not None else None,
        )
    except PromocionError as exc:
        raise _error(exc) from exc
    return await _state(_negocio(principal), cid)


@router.post("/connections/{connection_id}/promotion/verify", response_model=PromotionChecks)
async def promotion_verify(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> PromotionChecks:
    """Chequeos de producción con la app del negocio (nunca la de la plataforma), sin cambiar
    nada: el checklist por conexión de HU-CONN-07 más `own_app` y `real_number`."""
    cid = _connection_uuid(connection_id)
    async with _limitar_chequeos(_negocio(principal), cid):
        try:
            checks = await verificar_promocion(_negocio(principal), cid)
        except PromocionError as exc:
            raise _error(exc) from exc
    return PromotionChecks(checks=_checks_out(checks))


@router.post("/connections/{connection_id}/promotion/finish", response_model=PromotionResult)
async def promotion_finish(
    connection_id: str,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> PromotionResult:
    """Paso 4: si los chequeos de la conexión de producción con la app del negocio pasan, se da
    de baja la de prueba (sus conversaciones quedan en pausa: nunca se responden desde el
    número real). Si alguno falla, no cambia nada y vuelven los chequeos. `state` es el de la
    conexión de producción."""
    require_owner_or_master(principal)
    cid = _connection_uuid(connection_id)
    async with _limitar_chequeos(_negocio(principal), cid):
        try:
            promoted, checks, produccion_id = await terminar_promocion(_negocio(principal), cid)
        except PromocionError as exc:
            raise _error(exc) from exc
    # Ya promovida, la de prueba está eliminada: el estado es el de la de producción.
    return PromotionResult(
        promoted=promoted,
        checks=_checks_out(checks),
        state=await _state(_negocio(principal), produccion_id if promoted else cid),
    )
