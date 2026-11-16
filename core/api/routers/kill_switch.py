"""Interruptor global de la plataforma (master): `/api/v1/admin/platform/kill-switch` (HU-ARCH-38, ADR-0200).

Detiene en segundos todos los agentes y los envíos automáticos de todos los negocios, sin desplegar. Solo el
master estricto (`es_master`, no `actua_como_master`: un tester no lo opera). Un intento de alguien que no es
master recibe 403 y queda en el historial. Las rutas solo hablan con `core/admin/kill_switch_service.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.admin import kill_switch_service as svc
from core.admin.roles import es_master
from core.api.deps import Principal, get_principal
from core.api.errors import ApiError

router = APIRouter(prefix="/admin/platform/kill-switch", tags=["admin · plataforma"])

Accion = Literal["activated", "deactivated", "denied"]
Fuente = Literal["api", "cli"]


class KillSwitchEventOut(BaseModel):
    action: Accion
    reason: str | None
    actor_label: str | None
    source: Fuente
    created_at: datetime


class KillSwitchOut(BaseModel):
    active: bool
    reason: str | None
    changed_by_label: str | None
    source: Fuente | None
    changed_at: datetime | None
    history: list[KillSwitchEventOut]


class KillSwitchIn(BaseModel):
    active: bool
    # Obligatorio al activar: queda en el historial y en la alerta.
    reason: str | None = Field(default=None, max_length=svc.MAX_MOTIVO)


def _etiqueta(principal: Principal) -> str:
    return str(getattr(principal.admin, "email", "") or principal.admin.id)


async def _require_master(principal: Principal) -> None:
    if not es_master(principal.admin):
        await svc.registrar_denegado(admin_id=principal.admin.id, etiqueta=_etiqueta(principal))
        raise ApiError.forbidden("Solo el admin de plataforma puede usar el interruptor global.")


def _salida(e: svc.Estado) -> KillSwitchOut:
    return KillSwitchOut(
        active=e.active,
        reason=e.reason,
        changed_by_label=e.changed_by_label,
        source=e.source,  # type: ignore[arg-type]
        changed_at=e.changed_at,
        history=[
            KillSwitchEventOut(
                action=h.action, reason=h.reason, actor_label=h.actor_label,  # type: ignore[arg-type]
                source=h.source, created_at=h.created_at,  # type: ignore[arg-type]
            )
            for h in e.history
        ],
    )


@router.get("", response_model=KillSwitchOut)
async def obtener(principal: Annotated[Principal, Depends(get_principal)]) -> KillSwitchOut:
    await _require_master(principal)
    return _salida(await svc.estado())


@router.put("", response_model=KillSwitchOut)
async def fijar(body: KillSwitchIn, principal: Annotated[Principal, Depends(get_principal)]) -> KillSwitchOut:
    await _require_master(principal)
    try:
        estado = await svc.fijar(
            body.active, body.reason, admin_id=principal.admin.id, etiqueta=_etiqueta(principal), fuente="api"
        )
    except svc.MotivoRequerido as exc:
        raise ApiError.bad_request(str(exc), code="motivo_requerido") from exc
    return _salida(estado)
