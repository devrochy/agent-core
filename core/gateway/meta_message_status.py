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

Además, si el status trae `pricing` (objeto de facturación de Meta), se
persiste en `message_costs` -- ESE OBJETO NUNCA TRAE UN MONTO EN DÓLARES,
solo metadata (`billable`/`pricing_model`/categoría). El monto real vive
en `PricingDaily.cost` (única fuente autoritativa, `pricing_analytics`,
ver `core/gateway/meta_pricing_analytics.py`). Regla innegociable del
ticket: nunca se calcula un costo localmente con una tabla de tarifas
propia.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text

from core.db.models import Message, MessageCost, PhoneNumber, utcnow
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

_STATUS_RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}

# Valores DOCUMENTADOS conocidos -- de referencia únicamente, para loguear
# lo inesperado sin bloquear la ingesta (CLAUDE.md invariante nº7: un dato
# externo desconocido no se descarta ni se le inventa significado). Los
# enums reales de esta API NO están verificados contra doc primaria 2026.
_KNOWN_PRICING_TYPES = {"regular", "free_customer_service", "free_entry_point"}
_KNOWN_PRICING_CATEGORIES = {
    "authentication", "authentication-international", "marketing",
    "marketing_lite", "referral_conversion", "service", "utility",
}


async def _resolve_tenant_for_message(external_message_id: str) -> Any:
    async with get_session() as session:
        return (
            await session.execute(
                text("select resolve_tenant_for_message(:p)"), {"p": external_message_id}
            )
        ).scalar_one_or_none()


async def apply_status_update(status: dict[str, Any], *, phone_number_id: str | None = None) -> None:
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
        message_id: Any = None
        async with get_session() as session:
            db_message = (
                await session.execute(
                    select(Message).where(
                        Message.external_message_id == external_message_id, Message.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()

            if db_message is not None:
                message_id = db_message.id
                current_rank = _STATUS_RANK.get(db_message.status, -1)
                new_rank = _STATUS_RANK[new_status]
                is_newer_rank = new_rank > current_rank
                is_newer_ts = (
                    event_ts is not None
                    and db_message.status_updated_at is not None
                    and event_ts > db_message.status_updated_at
                )
                if is_newer_rank or is_newer_ts:
                    db_message.status = new_status
                    db_message.status_updated_at = event_ts or utcnow()

                    error = status.get("errors") or []
                    if new_status == "failed" and error:
                        first = error[0]
                        db_message.error_code = first.get("code")
                        db_message.error_subcode = first.get("error_subcode") or first.get("error_data", {}).get("subcode")
                        db_message.error_reason = first.get("message") or first.get("title")
                        db_message.error_fbtrace_id = first.get("fbtrace_id")

        if status.get("pricing") is not None:
            await _persist_message_cost(
                status, tenant_id=tenant_id, message_id=message_id,
                external_message_id=external_message_id, phone_number_id=phone_number_id,
                event_ts=event_ts, new_status=new_status,
            )


async def _persist_message_cost(
    status: dict[str, Any], *, tenant_id: Any, message_id: Any, external_message_id: str,
    phone_number_id: str | None, event_ts: datetime | None, new_status: str,
) -> None:
    pricing = status.get("pricing") or {}
    pricing_type = pricing.get("pricing_type") or pricing.get("type")
    pricing_category = pricing.get("category")
    if pricing_type is not None and str(pricing_type) not in _KNOWN_PRICING_TYPES:
        log.warning("pricing_type_desconocido", pricing_type=pricing_type, external_message_id=external_message_id)
    if pricing_category is not None and str(pricing_category) not in _KNOWN_PRICING_CATEGORIES:
        log.warning(
            "pricing_category_desconocida", pricing_category=pricing_category, external_message_id=external_message_id
        )

    conversation = status.get("conversation") or {}
    origin = conversation.get("origin") or {}
    expiration_at: datetime | None = None
    raw_expiration = conversation.get("expiration_timestamp")
    if raw_expiration is not None:
        try:
            expiration_at = datetime.fromtimestamp(int(raw_expiration), tz=UTC)
        except (TypeError, ValueError):
            expiration_at = None

    waba_account_id = None
    if phone_number_id:
        async with get_session() as session:
            phone = (
                await session.execute(
                    select(PhoneNumber).where(
                        PhoneNumber.phone_number_id == phone_number_id, PhoneNumber.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            waba_account_id = phone.waba_account_id if phone else None

    async with get_session() as session:
        session.add(
            MessageCost(
                tenant_id=tenant_id, message_id=message_id, external_message_id=external_message_id,
                waba_account_id=waba_account_id, phone_number_id=phone_number_id,
                billable=pricing.get("billable"), pricing_model=pricing.get("pricing_model"),
                pricing_type=str(pricing_type) if pricing_type is not None else None,
                pricing_category=str(pricing_category) if pricing_category is not None else None,
                meta_conversation_id=conversation.get("id"),
                conversation_origin_type=origin.get("type"),
                conversation_expiration_at=expiration_at,
                status=new_status, event_ts=event_ts or utcnow(), raw=pricing,
            )
        )
