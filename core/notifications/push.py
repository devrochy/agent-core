"""Cliente de Expo Push Service y envío de push de la app oficial (ADR-0099).

Separación de responsabilidades:

- `_post_expo`: HTTP puro al servicio de Expo (mockeable en tests).
- `enviar_push`: resuelve los tokens activos dentro de `tenant_scope`, arma el
  lote y lo envía; los tickets con error `DeviceNotRegistered` se dan de baja
  (soft delete) para no reintentar infinitamente. Corre dentro de una tarea
  Celery (`core/tasks/push_tasks`), nunca en el request.
- `PushTransportError`: fallo de red/servicio que justifica reintento del task.

Sin `EXPO_PUSH_ENABLED`, el envío es log-only: un canal sin configurar no rompe
el flujo (mismo criterio que el correo de sistema, ADR-0098).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select, text

from core.config import get_settings
from core.db.models import PushToken
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)

DEVICE_NOT_REGISTERED = "DeviceNotRegistered"

# Canal de Android que crea la app oficial (`CANAL_DEFAULT` en solvershub-apk,
# `src/modules/notificaciones/push.ts`). Sin él, Android usa el canal de respaldo de
# FCM, sin la importancia ni el sonido de la app. Expo lo ignora en iOS.
ANDROID_CHANNEL_ID = "solvershub-general"

# Máximo de mensajes por request del servicio de Expo.
EXPO_CHUNK = 100
# Vida del mensaje en Expo si el dispositivo está apagado: un aviso viejo no sirve.
TTL_NORMAL_S = 24 * 3600
TTL_CRITICO_S = 3 * 24 * 3600


def canal_android(category: str | None) -> str:
    """Canal de Android de la categoría (la app los crea por JS); sin categoría, el general."""
    return f"solvershub-{category}" if category else ANDROID_CHANNEL_ID


def prioridad_expo(severity: str | None) -> str:
    """`high` despierta el dispositivo (FCM): solo para lo que pide acción pronto."""
    return "high" if severity in ("warning", "critical") else "default"


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
    *,
    category: str | None = None,
    severity: str | None = None,
    badges: Mapping[uuid.UUID, int] | None = None,
) -> None:
    """Resuelve tokens de los admins en el negocio y envía el push (o solo loguea).

    `category`/`severity` (centro de notificaciones, ADR-0167) fijan el canal de Android,
    la prioridad y el TTL; `badges` da el contador de no leídas por admin. Sin ellos, el
    mensaje sale como antes (canal general). Envía en lotes de `EXPO_CHUNK`.
    """
    settings = get_settings()
    if not settings.expo_push_enabled:
        log.info(
            "push_disabled",
            tenant_id=str(tenant_id),
            admin_ids=[str(a) for a in admin_ids],
            # sin el título: puede llevar datos del cliente
            kind=str((data or {}).get("kind") or (data or {}).get("type") or ""),
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

        extra: dict[str, Any] = {}
        if category or severity:
            extra = {
                "priority": prioridad_expo(severity),
                "ttl": TTL_CRITICO_S if severity == "critical" else TTL_NORMAL_S,
            }
        for inicio in range(0, len(tokens), EXPO_CHUNK):
            lote = tokens[inicio : inicio + EXPO_CHUNK]
            mensajes = []
            for t in lote:
                mensaje: dict[str, Any] = {
                    "to": t.token,
                    "title": title,
                    "body": body,
                    "data": {**data, "tenantId": str(tenant_id)},
                    "sound": "default",
                    "channelId": canal_android(category),
                    **extra,
                }
                if badges is not None and t.admin_id in badges:
                    mensaje["badge"] = badges[t.admin_id]
                mensajes.append(mensaje)
            tickets = await _post_expo(mensajes)

            for token_row, ticket in zip(lote, tickets, strict=True):
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
            # Una baja ya aplicada no se pierde si un lote posterior falla y se reintenta.
            await session.commit()
            # El scope del negocio es de transacción y murió con el COMMIT: se vuelve a fijar
            # para que el siguiente lote siga bajo la RLS del mismo negocio.
            await session.execute(
                text("select set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)}
            )

    log.info(
        "push_sent",
        tenant_id=str(tenant_id),
        destinatarios=len(tokens),
    )
