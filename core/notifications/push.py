"""Cliente de Expo Push Service y envío de push de la app oficial (ADR-0095).

Separación de responsabilidades:

- `_post_expo`: HTTP puro al servicio de Expo (mockeable en tests).
- `enviar_push`: resuelve los tokens activos dentro de `tenant_scope`, arma el
  lote y lo envía; los tickets con error `DeviceNotRegistered` se dan de baja
  (soft delete) para no reintentar infinitamente. Corre dentro de una tarea
  Celery (`core/tasks/push_tasks`), nunca en el request.
- `PushTransportError`: fallo de red/servicio que justifica reintento del task.

Sin `EXPO_PUSH_ENABLED`, el envío es log-only: un canal sin configurar no rompe
el flujo (mismo criterio que el correo de sistema, ADR-0094).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select

from core.config import get_settings
from core.db.models import PushToken
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

DEVICE_NOT_REGISTERED = "DeviceNotRegistered"


class PushTransportError(RuntimeError):
    """No se pudo entregar al servicio de Expo (red, 5xx, formato)."""


async def _post_expo(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Envía el lote y devuelve los tickets de Expo (uno por mensaje)."""
    settings = get_settings()
    headers = {"Content-Type": "application/json"}
    if settings.expo_push_access_token:
        headers["Authorization"] = f"Bearer {settings.expo_push_access_token}"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(settings.expo_push_url, json=payloads, headers=headers)
    if resp.status_code >= 500:
        raise PushTransportError(f"expo {resp.status_code}")
    if resp.status_code != 200:
        raise PushTransportError(f"expo {resp.status_code}")
    body = resp.json()
    tickets = body.get("data") or []
    if len(tickets) != len(payloads):
        raise PushTransportError("expo devolvió un número distinto de tickets")
    return tickets


async def enviar_push(
    tenant_id: uuid.UUID,
    admin_ids: list[uuid.UUID],
    title: str,
    body: str,
    data: dict[str, Any],
) -> None:
    """Resuelve tokens de los admins en el negocio y envía el push (o solo loguea)."""
    settings = get_settings()
    if not settings.expo_push_enabled:
        log.info(
            "push_disabled",
            tenant_id=str(tenant_id),
            admin_ids=[str(a) for a in admin_ids],
            title=title[:80],
        )
        return

    if not admin_ids:
        return

    async with get_session() as session:
        tokens = (
            await session.execute(
                select(PushToken).where(
                    PushToken.tenant_id == tenant_id,
                    PushToken.admin_id.in_(admin_ids),
                    PushToken.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if not tokens:
            return

        mensajes = [
            {
                "to": t.token,
                "title": title,
                "body": body,
                "data": {**data, "tenantId": str(tenant_id)},
                "sound": "default",
            }
            for t in tokens
        ]
        tickets = await _post_expo(mensajes)

        for token_row, ticket in zip(tokens, tickets, strict=True):
            if ticket.get("status") == "ok":
                continue
            detail = (ticket.get("details") or {}).get("error")
            if detail == DEVICE_NOT_REGISTERED:
                # Baja SOFT (invariante 8: sin borrado físico fuera del purge).
                token_row.deleted_at = datetime.now(UTC)
                log.info(
                    "push_token_baja",
                    tenant_id=str(tenant_id),
                    admin_id=str(token_row.admin_id),
                    motivo=detail,
                )
            else:
                log.warning(
                    "push_ticket_error",
                    tenant_id=str(tenant_id),
                    error=detail or ticket.get("status"),
                )
        await session.commit()

    log.info(
        "push_sent",
        tenant_id=str(tenant_id),
        destinatarios=len(tokens),
        title=title[:80],
    )
