"""Máquina de estados de `Message.status` alimentada por el webhook
`statuses` de WhatsApp (sent/delivered/read/failed) -- el ORDEN NO está
garantizado (Meta puede reentregar o reordenar), así que un estado nuevo
solo se aplica si es estrictamente posterior en la máquina
queued < sent < delivered < read < failed, o si el timestamp del evento es
posterior al de la última actualización -- nunca se sobrescribe por mero
orden de llegada.

El payload solo trae el `wamid` (`external_message_id`), no el tenant --
mismo problema chicken-and-egg ya resuelto para plantillas (ADR-0031):
`resolve_tenant_for_message` (SECURITY DEFINER, migración 0027) resuelve el
tenant antes de abrir `tenant_scope`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text

from core.db.models import Message, utcnow
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

_STATUS_RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}


async def _resolve_tenant_for_message(external_message_id: str) -> Any:
    async with get_session() as session:
        return (
            await session.execute(
                text("select resolve_tenant_for_message(:p)"), {"p": external_message_id}
            )
        ).scalar_one_or_none()


async def apply_status_update(status: dict[str, Any]) -> None:
    external_message_id = str(status.get("id") or "")
    new_status = str(status.get("status") or "")
    if not external_message_id or new_status not in _STATUS_RANK:
        log.warning("status_update_payload_invalido", status=status)
        return

    event_ts: datetime | None = None
    raw_ts = status.get("timestamp")
    if raw_ts is not None:
        try:
            event_ts = datetime.fromtimestamp(int(raw_ts), tz=UTC)
        except (TypeError, ValueError):
            event_ts = None

    tenant_id = await _resolve_tenant_for_message(external_message_id)
    if tenant_id is None:
        log.info("status_update_sin_fila_local", external_message_id=external_message_id, status=new_status)
        return

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            db_message = (
                await session.execute(
                    select(Message).where(
                        Message.external_message_id == external_message_id, Message.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if db_message is None:
                return

            current_rank = _STATUS_RANK.get(db_message.status, -1)
            new_rank = _STATUS_RANK[new_status]
            is_newer_rank = new_rank > current_rank
            is_newer_ts = (
                event_ts is not None
                and db_message.status_updated_at is not None
                and event_ts > db_message.status_updated_at
            )
            if not (is_newer_rank or is_newer_ts):
                return

            db_message.status = new_status
            db_message.status_updated_at = event_ts or utcnow()

            error = status.get("errors") or []
            if new_status == "failed" and error:
                first = error[0]
                db_message.error_code = first.get("code")
                db_message.error_subcode = first.get("error_subcode") or first.get("error_data", {}).get("subcode")
                db_message.error_reason = first.get("message") or first.get("title")
                db_message.error_fbtrace_id = first.get("fbtrace_id")
