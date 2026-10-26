"""Tareas Celery de notificaciones push (ADR-0099).

El envío nunca ocurre en el request: los disparadores (escalación, sync de
campo) encolan estas tareas y el worker resuelve tokens y habla con Expo.
Patrón idéntico a `outbound_send`: task síncrono + `run_async` para usar el
loop persistente del worker.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.notifications.push import PushTransportError, enviar_push
from core.shared.async_bridge import run_async
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.celery_app import celery

log = get_logger(__name__)

MAX_ATTEMPTS = 3

TITULO_ESCALACION = "Nueva escalación"
TITULO_CAMPO = "Actividad de campo"


@celery.task(
    bind=True,
    max_retries=MAX_ATTEMPTS,
    default_retry_delay=30,
    name="core.push.send_batch",
)
def send_push_batch(
    self,
    tenant_id: str,
    admin_ids: list[str],
    title: str,
    body: str,
    data: dict[str, Any],
) -> None:
    """Envía el push a los tokens de `admin_ids` en el negocio `tenant_id`."""

    async def _send() -> None:
        tid = uuid.UUID(tenant_id)
        async with tenant_scope(tid):
            await enviar_push(tid, [uuid.UUID(a) for a in admin_ids], title, body, data)

    try:
        run_async(_send())
    except PushTransportError as exc:
        if self.request.retries >= MAX_ATTEMPTS:
            log.warning("push_fail_permanente", tenant_id=tenant_id, error=str(exc))
            return
        raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1)) from exc
    except Exception:
        log.warning("push_error", tenant_id=tenant_id, exc_info=True)


@celery.task(
    bind=True,
    max_retries=MAX_ATTEMPTS,
    default_retry_delay=30,
    name="core.push.notify_owners",
)
def notify_owners_push(
    self,
    tenant_id: str,
    title: str,
    body: str,
    data: dict[str, Any],
) -> None:
    """Envía el push a los dueños del negocio (TenantUser.role = owner)."""

    async def _owners() -> list[str]:
        from sqlalchemy import select

        from core.db.models import TenantUser
        from core.db.session import get_session

        tid = uuid.UUID(tenant_id)
        async with tenant_scope(tid):
            async with get_session() as session:
                rows = (
                    await session.execute(
                        select(TenantUser.admin_id).where(
                            TenantUser.tenant_id == tid,
                            TenantUser.role == "owner",
                        )
                    )
                ).scalars().all()
            return [str(a) for a in rows]

    owner_ids = run_async(_owners())
    if not owner_ids:
        return
    send_push_batch.delay(tenant_id, owner_ids, title, body, data)


@celery.task(
    bind=True,
    max_retries=MAX_ATTEMPTS,
    default_retry_delay=30,
    name="core.push.send_notification",
)
def send_notification_push(self, tenant_id: str, notification_id: str) -> None:
    """Envío inmediato del push de una notificación urgente (ADR-0167).

    Idempotente por `push_sent_at`: si el despachador del outbox se adelanta o reintenta, no
    se repite. Un fallo de transporte reintenta; el outbox queda como red de seguridad."""
    from core.notifications.service import enviar_push_de_notificacion

    async def _send() -> None:
        tid = uuid.UUID(tenant_id)
        async with tenant_scope(tid):
            await enviar_push_de_notificacion(uuid.UUID(notification_id))

    try:
        run_async(_send())
    except PushTransportError as exc:
        if self.request.retries >= MAX_ATTEMPTS:
            log.warning("push_notificacion_fail_permanente", tenant_id=tenant_id, error=str(exc))
            return
        raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1)) from exc
    except Exception:
        log.warning("push_notificacion_error", tenant_id=tenant_id, exc_info=True)
