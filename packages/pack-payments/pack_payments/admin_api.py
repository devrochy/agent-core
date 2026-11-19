"""API admin de las cuentas Wompi del negocio (ADR-0190 §6, HU-AOS-62/66), consumida por el panel (Conexiones › Pagos).

- `GET    /api/v1/pack/payments/accounts` — cuentas vivas, sin llave privada ni secreto de eventos, con los agentes
  que usa cada una y la URL de eventos que hay que pegar en Wompi.
- `POST   /api/v1/pack/payments/accounts` — crea una cuenta.
- `PATCH  /api/v1/pack/payments/accounts/{id}` — edita; los secretos que no vienen se conservan (rotar = mandarlos).
- `DELETE /api/v1/pack/payments/accounts/{id}` — borrado lógico; 409 `account_in_use` con los agentes si alguno la usa.

Permisos: la sección `plugins` (la de las habilidades), lectura o escritura. Los secretos nunca vuelven en la respuesta
ni se loguean.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict, Field

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.config import get_settings
from core.shared.tenant import tenant_scope, tenant_slug
from pack_payments import cuentas

router = APIRouter(prefix="/api/v1/pack/payments", tags=["pack · pagos"])


class PaymentAccount(BaseModel):
    id: uuid.UUID
    name: str
    public_key: str
    sandbox: bool
    link_expires_hours: int
    min_amount: float | None
    # La URL de eventos de esta cuenta para pegar en el panel de Wompi.
    events_url: str
    agent_ids: list[uuid.UUID]


class PaymentAccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    public_key: str = Field(min_length=10, max_length=200)
    private_key: str = Field(min_length=10, max_length=200)
    events_secret: str = Field(min_length=4, max_length=200)
    sandbox: bool = True
    link_expires_hours: int = Field(default=24, ge=1, le=720)
    min_amount: float | None = Field(default=None, ge=0)


class PaymentAccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    public_key: str | None = Field(default=None, min_length=10, max_length=200)
    private_key: str | None = Field(default=None, min_length=10, max_length=200)
    events_secret: str | None = Field(default=None, min_length=4, max_length=200)
    sandbox: bool | None = None
    link_expires_hours: int | None = Field(default=None, ge=1, le=720)
    min_amount: float | None = Field(default=None, ge=0)


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.forbidden("Elige un negocio.")
    return principal.tenant_id


async def _salida(tid: uuid.UUID, c: cuentas.CuentaVista) -> PaymentAccount:
    base = (get_settings().public_base_url or "").rstrip("/")
    slug = await tenant_slug(tid)
    return PaymentAccount(
        id=c.id,
        name=c.nombre,
        public_key=c.public_key,
        sandbox=c.sandbox,
        link_expires_hours=c.link_expira_horas,
        min_amount=c.monto_minimo,
        events_url=f"{base}/webhooks/payments/wompi/{slug}/{c.id}",
        agent_ids=await cuentas.agentes_que_usan(c.id),
    )


@router.get("/accounts", response_model=list[PaymentAccount])
async def list_accounts(principal: Annotated[Principal, Depends(require("plugins"))]) -> list[PaymentAccount]:
    tid = _tenant(principal)
    async with tenant_scope(tid):
        return [await _salida(tid, c) for c in await cuentas.listar_cuentas()]


@router.post("/accounts", response_model=PaymentAccount, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: PaymentAccountIn, principal: Annotated[Principal, Depends(require("plugins", write=True))]
) -> PaymentAccount:
    tid = _tenant(principal)
    async with tenant_scope(tid):
        try:
            c = await cuentas.crear_cuenta(
                nombre=payload.name,
                public_key=payload.public_key,
                private_key=payload.private_key,
                events_secret=payload.events_secret,
                sandbox=payload.sandbox,
                link_expira_horas=payload.link_expires_hours,
                monto_minimo=payload.min_amount,
            )
        except Exception as exc:  # nombre repetido (índice único): sin detalles de la base
            if "uq_pack_payments_accounts_nombre" in str(exc):
                raise ApiError(409, "name_taken", "Ya tienes una cuenta con ese nombre.") from None
            raise
        return await _salida(tid, c)


@router.patch("/accounts/{account_id}", response_model=PaymentAccount)
async def update_account(
    account_id: uuid.UUID,
    payload: PaymentAccountPatch,
    principal: Annotated[Principal, Depends(require("plugins", write=True))],
) -> PaymentAccount:
    tid = _tenant(principal)
    async with tenant_scope(tid):
        try:
            c = await cuentas.actualizar_cuenta(
                account_id,
                nombre=payload.name,
                public_key=payload.public_key,
                private_key=payload.private_key,
                events_secret=payload.events_secret,
                sandbox=payload.sandbox,
                link_expira_horas=payload.link_expires_hours,
                monto_minimo=payload.min_amount,
            )
        except cuentas.CuentaNoEncontrada:
            raise ApiError.not_found("Cuenta no encontrada.") from None
        return await _salida(tid, c)


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: uuid.UUID, principal: Annotated[Principal, Depends(require("plugins", write=True))]
) -> Response:
    tid = _tenant(principal)
    async with tenant_scope(tid):
        try:
            await cuentas.borrar_cuenta(account_id)
        except cuentas.CuentaEnUso as exc:
            raise ApiError(
                409,
                "account_in_use",
                "Esta cuenta la usan agentes: elige otra cuenta en ellos antes de borrarla.",
                details={"agent_ids": [str(a) for a in exc.agentes]},
            ) from None
        except cuentas.CuentaNoEncontrada:
            raise ApiError.not_found("Cuenta no encontrada.") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
