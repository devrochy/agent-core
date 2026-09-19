"""Aplana el payload de webhook de Meta (WhatsApp/Messenger/Instagram) en
eventos individuales direccionables, para poder rutear tenant y deduplicar
por evento en vez de por payload completo.

WhatsApp (`object: whatsapp_business_account`) usa `entry[].changes[].value`;
Messenger/Instagram (`object: page` / `instagram`) usan `entry[].messaging[]`
directo, sin `changes[]`/`field` -- se les asigna `field="messaging"`
sintético acá para que `FlattenedEvent` tenga una forma uniforme.

Itera SIEMPRE todas las `entry[]` -- nunca asume `entry[0]` (un payload
puede traer WABAs/páginas de tenants distintos si comparten la misma App
de Meta, agrupadas por Meta en un solo POST)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel


class FlattenedEvent(BaseModel):
    object_type: str
    field: str
    external_id: str
    entry_id: str
    sub_id: str | None
    value: dict[str, Any]


def _hash_value(field: str, value: dict[str, Any]) -> str:
    """Fallback determinístico para eventos sin id propio de Meta: mismo
    `value` -> mismo hash entre llamadas (claves ordenadas)."""
    canonical = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(f"{field}:{canonical}".encode()).hexdigest()


def _flatten_whatsapp_change(entry_id: str, change: dict[str, Any]) -> list[FlattenedEvent]:
    field = change.get("field", "")
    value = change.get("value", {}) or {}
    phone_number_id = value.get("metadata", {}).get("phone_number_id")
    events: list[FlattenedEvent] = []

    # 'messages' y 'statuses' comparten field == "messages" -- se discrimina
    # por la CLAVE presente en `value`, nunca por `field`.
    for msg in value.get("messages", []):
        events.append(
            FlattenedEvent(
                object_type="whatsapp_business_account",
                field=field,
                external_id=str(msg.get("id", "")) or _hash_value(field, msg),
                entry_id=entry_id,
                sub_id=phone_number_id,
                value=value,
            )
        )

    for status in value.get("statuses", []):
        # mismo wamid llega como sent/delivered/read -- son eventos DISTINTOS.
        external_id = f"{status.get('id', '')}:{status.get('status', '')}"
        events.append(
            FlattenedEvent(
                object_type="whatsapp_business_account",
                field=field,
                external_id=external_id,
                entry_id=entry_id,
                sub_id=phone_number_id,
                value=value,
            )
        )

    if not value.get("messages") and not value.get("statuses"):
        # cualquier otro tipo de `value` (ej. account_alerts, template
        # status updates): fallback sha256 sobre todo el `value`.
        events.append(
            FlattenedEvent(
                object_type="whatsapp_business_account",
                field=field,
                external_id=_hash_value(field, value),
                entry_id=entry_id,
                sub_id=phone_number_id,
                value=value,
            )
        )

    return events


def _flatten_messaging_entry(object_type: str, entry_id: str, messaging_event: dict[str, Any]) -> FlattenedEvent:
    mid = messaging_event.get("message", {}).get("mid")
    external_id = str(mid) if mid else _hash_value("messaging", messaging_event)
    return FlattenedEvent(
        object_type=object_type,
        field="messaging",
        external_id=external_id,
        entry_id=entry_id,
        sub_id=None,
        value=messaging_event,
    )


def flatten_payload(payload: dict[str, Any]) -> list[FlattenedEvent]:
    object_type = payload.get("object", "")
    events: list[FlattenedEvent] = []

    for entry in payload.get("entry", []):
        entry_id = str(entry.get("id", ""))
        if not entry_id:
            continue

        if object_type == "whatsapp_business_account":
            for change in entry.get("changes", []):
                events.extend(_flatten_whatsapp_change(entry_id, change))
        elif object_type in ("page", "instagram"):
            for messaging_event in entry.get("messaging", []):
                events.append(_flatten_messaging_entry(object_type, entry_id, messaging_event))
        # object_type desconocido: no se descarta silenciosamente el payload
        # entero, pero tampoco hay reglas de aplanado para él todavía -- el
        # caller decide (hoy: se ignora, no hay tenant que resolver).

    return events
