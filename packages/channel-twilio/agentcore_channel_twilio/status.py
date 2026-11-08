"""Status callback de Twilio → estado del mensaje del core (ADR-0186).

Devuelve el mismo dict que entiende `core.gateway.meta_message_status.apply_status_update`
(`id`, `status`, `errors`), así la máquina `queued < sent < delivered < read` y `failed` es una
sola para los dos proveedores y un estado nunca retrocede.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# `sending` y los estados previos al envío cuentan como `queued`; `undelivered` y `canceled`,
# como `failed`. Lo que Twilio agregue y no esté aquí se ignora (se loguea en el core).
STATUS_MAP = {
    "accepted": "queued",
    "scheduled": "queued",
    "queued": "queued",
    "sending": "queued",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "undelivered": "failed",
    "failed": "failed",
    "canceled": "failed",
}


def parse_status(payload: Mapping[str, str]) -> dict[str, Any] | None:
    """`None` si el callback no trae `MessageSid` o trae un estado desconocido."""
    message_sid = str(payload.get("MessageSid") or payload.get("SmsSid") or "")
    provider_status = str(payload.get("MessageStatus") or payload.get("SmsStatus") or "").lower()
    status = STATUS_MAP.get(provider_status)
    if not message_sid or status is None:
        return None
    update: dict[str, Any] = {
        "id": message_sid,
        "status": status,
        "provider_status": provider_status,
    }
    raw_code = str(payload.get("ErrorCode") or "").strip()
    if status == "failed" and raw_code:
        # `messages.error_code` es entero: un código no numérico se pierde, el mensaje queda.
        code = int(raw_code) if raw_code.isdigit() else None
        update["errors"] = [{"code": code, "title": str(payload.get("ErrorMessage") or "")}]
    return update
