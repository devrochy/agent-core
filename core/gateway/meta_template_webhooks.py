"""Máquina de estados de plantillas alimentada por webhooks -- ÚNICA fuente
de verdad del estado (el sync paginado es solo fallback). Nunca toca el log
crudo de webhooks (ADR-0028): ya se registró ahí antes de llamar a estos
handlers.

Los tres eventos traen solo `message_template_id`. La entry ya viene ruteada a su
negocio (ADR-0100), así que la plantilla se busca bajo el `tenant_scope` de ese negocio
(ADR-0122: el id es único por negocio, no global).

Cada handler recibe `expected_tenant_id`: el negocio de la entry del webhook, ya
autorizado por su firma. Si la plantilla es de OTRO negocio, el evento se descarta:
con una firma válida propia, un negocio podría cambiar el estado de una plantilla
ajena (ADR-0100).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from core.db.models import MessageTemplate, utcnow
from core.db.session import get_session
from core.memory.working import get_redis
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

# Los 14 valores documentados de `event` en `message_template_status_update`
# (no seis). El `status` de la plantilla se pisa LITERAL con el valor que
# manda Meta -- nunca se reinterpreta (CLAUDE.md invariante nº7), incluido
# 'UNARCHIVED' aunque hoy no sea un status "real" documentado.
TemplateStatusEvent = Literal[
    "APPROVED", "ARCHIVED", "UNARCHIVED", "DELETED", "DISABLED", "FLAGGED", "IN_APPEAL",
    "LIMIT_EXCEEDED", "LOCKED", "PAUSED", "PENDING", "REINSTATED", "PENDING_DELETION", "REJECTED",
]
_ALL_STATUS_EVENTS: frozenset[str] = frozenset(TemplateStatusEvent.__args__)  # type: ignore[attr-defined]


class TemplateStatusUpdatePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    message_template_id: int | str
    message_template_name: str | None = None
    message_template_language: str | None = None
    event: str
    reason: str | None = None
    # No documentados con certeza en 2026 -- se aceptan si vienen, se ignoran si no.
    rejection_info: dict[str, Any] | None = None
    disable_info: dict[str, Any] | None = None
    other_info: dict[str, Any] | None = None


class _PassthroughPayload(BaseModel):
    """Para los dos webhooks NO verificados (`template_category_update`,
    `message_template_quality_update`) -- equivalente Python de
    `.zod().passthrough()`: acepta cualquier campo extra sin fallar."""

    model_config = ConfigDict(extra="allow")

    message_template_id: int | str | None = None


async def _log_raw_payload_once(event_name: str, payload: dict[str, Any]) -> None:
    """Loguea el payload crudo la PRIMERA vez que se ve cada tipo de evento
    no verificado -- para juntar datos reales sin inundar logs después."""
    redis = get_redis()
    seen = await redis.set(f"_seen_webhook_payload:{event_name}", "1", nx=True)
    if seen:
        log.info("template_webhook_payload_no_verificado_primera_vez", event_name=event_name, payload=payload)


async def _plantilla_es_del_negocio(meta_template_id: str, tenant_id: uuid.UUID, *, event: str) -> bool:
    """¿El negocio de la entry tiene esta plantilla? Se busca bajo SU `tenant_scope` (RLS).

    Desde ADR-0122 el `meta_template_id` es único por negocio, no global: si otro negocio
    reclamó una WABA eliminada, los dos tienen la fila. La función SECURITY DEFINER de antes
    (`resolve_tenant_for_message_template`, `LIMIT 1` global) elegía una al azar y descartaba
    los cambios del dueño nuevo. Una plantilla de otro negocio no se encuentra acá (ADR-0100)."""
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            existe = (
                await session.execute(
                    select(MessageTemplate.id)
                    .where(MessageTemplate.tenant_id == tenant_id, MessageTemplate.meta_template_id == meta_template_id)
                    .limit(1)
                )
            ).first()
    if existe is None:
        log.info(
            "template_webhook_sin_fila_del_negocio",
            meta_template_id=meta_template_id, webhook_field=event, tenant_id=str(tenant_id),
        )
        return False
    return True


async def handle_template_status_update(payload: dict[str, Any], *, expected_tenant_id: uuid.UUID) -> None:
    parsed = TemplateStatusUpdatePayload.model_validate(payload)
    if parsed.event not in _ALL_STATUS_EVENTS:
        raise AssertionError(f"evento de message_template_status_update no manejado: {parsed.event!r}")

    if not await _plantilla_es_del_negocio(
        str(parsed.message_template_id), expected_tenant_id, event=parsed.event
    ):
        return
    tenant_id = expected_tenant_id

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            db_row = (
                await session.execute(
                    select(MessageTemplate).where(
                        MessageTemplate.meta_template_id == str(parsed.message_template_id),
                        MessageTemplate.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if db_row is None:
                return
            db_row.status = parsed.event
            if parsed.event == "REJECTED" and parsed.rejection_info:
                db_row.rejected_reason = parsed.rejection_info.get("reason")
                db_row.rejection_recommendation = parsed.rejection_info.get("recommendation")

                from core.shared.alerts import send_alert

                await send_alert(
                    f"Plantilla rechazada: {parsed.message_template_name or parsed.message_template_id}",
                    parsed.rejection_info.get("recommendation") or parsed.rejection_info.get("reason") or "Sin detalle de Meta.",
                    severity="warning",
                )
            if parsed.event == "DISABLED" and parsed.disable_info:
                db_row.disable_date = parsed.disable_info.get("disable_date")
            if parsed.other_info:
                db_row.other_info_title = parsed.other_info.get("title")
                db_row.other_info_description = parsed.other_info.get("description")
            db_row.updated_at = utcnow()


async def handle_template_category_update(payload: dict[str, Any], *, expected_tenant_id: uuid.UUID) -> None:
    """Payload NO verificado contra doc primaria de 2026 -- parseo
    defensivo, se loguea crudo la primera vez (de una plantilla del negocio de la
    entry: uno ajeno no gasta ese aviso)."""
    parsed = _PassthroughPayload.model_validate(payload)
    if parsed.message_template_id is None:
        return

    if not await _plantilla_es_del_negocio(
        str(parsed.message_template_id), expected_tenant_id, event="template_category_update"
    ):
        return
    tenant_id = expected_tenant_id
    await _log_raw_payload_once("template_category_update", payload)

    new_category = payload.get("new_category") or payload.get("category")
    if not new_category:
        log.warning("template_category_update_sin_new_category", payload=payload)
        return

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            db_row = (
                await session.execute(
                    select(MessageTemplate).where(
                        MessageTemplate.meta_template_id == str(parsed.message_template_id),
                        MessageTemplate.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if db_row is None:
                return
            db_row.previous_category = db_row.category
            db_row.category = str(new_category)
            db_row.category_changed_at = utcnow()


async def handle_template_quality_update(payload: dict[str, Any], *, expected_tenant_id: uuid.UUID) -> None:
    """Payload NO verificado -- se espera `previous_quality_score`/
    `new_quality_score` (GREEN|YELLOW|RED|UNKNOWN), pero no se asumen esos
    nombres de campo exactos, se usa `.get()` defensivo."""
    parsed = _PassthroughPayload.model_validate(payload)
    if parsed.message_template_id is None:
        return

    if not await _plantilla_es_del_negocio(
        str(parsed.message_template_id), expected_tenant_id, event="message_template_quality_update"
    ):
        return
    tenant_id = expected_tenant_id
    await _log_raw_payload_once("message_template_quality_update", payload)

    new_quality = payload.get("new_quality_score")
    previous_quality = payload.get("previous_quality_score")

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            db_row = (
                await session.execute(
                    select(MessageTemplate).where(
                        MessageTemplate.meta_template_id == str(parsed.message_template_id),
                        MessageTemplate.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if db_row is None:
                return
            if previous_quality is not None:
                db_row.previous_quality_score = str(previous_quality)
            if new_quality is not None:
                db_row.quality_score = str(new_quality)
