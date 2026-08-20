"""Registro de auditoría inmutable.

Principio (heredado de agent-sale): el audit_log registra lo que la tool
REALMENTE hizo, no lo que el modelo intentó. Insert best-effort: un fallo
de auditoría nunca interrumpe la respuesta al cliente.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.db.models import AuditLog
from core.db.session import get_session
from core.shared.logger import get_logger

log = get_logger(__name__)


async def record_audit(
    actor: str,
    action: str,
    conversation_id: uuid.UUID | None = None,
    input_data: dict[str, Any] | None = None,
    output_data: dict[str, Any] | None = None,
) -> None:
    try:
        async with get_session() as session:
            session.add(
                AuditLog(
                    conversation_id=conversation_id,
                    actor=actor,
                    action=action,
                    input=input_data,
                    output=output_data,
                )
            )
    except Exception:
        log.exception("audit_write_failed", actor=actor, action=action)
