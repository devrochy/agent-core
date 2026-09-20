"""Endpoints de webhooks de canales.

Flujo (patrón gateway -> queue -> consumer de agent-sale):
1. Verificar firma (HMAC-SHA256 con App Secret).
2. Aplanar el payload en eventos individuales (`webhook_flatten.py`) y
   resolver el tenant de cada uno (`webhook_route_tenant.py`) -- un payload
   puede traer entries de varios tenants si comparten la misma App de Meta.
3. Registrar cada evento en `webhook_events` (auditoría + idempotencia por
   `(meta_app_id, external_id)`) -- si esto falla, 500 a propósito (Meta
   reintenta) y se alerta: es la única red de seguridad ante una BD caída.
4. Parsear -> InboundMessage[], deduplicar (Redis SET NX, 48h), procesar
   medios si mod-media-processor está activo, encolar en Redis Stream.
5. Responder 200 (o "ignored" con 200 si no hay nada que hacer: un payload
   inválido NUNCA debe devolver un error que dispare 36h de reintentos de Meta
   en vano -- la única excepción real es la falla de `webhook_events`).

El LLM nunca corre dentro del request del webhook.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.config import get_settings
from core.gateway.channels.registry import inbound_adapter_for
from core.gateway.connections import effective_credentials
from core.gateway.idempotency import claim_message_once
from core.gateway.webhook_flatten import FlattenedEvent, flatten_payload
from core.gateway.webhook_route_tenant import resolve_tenant_for_event
from core.gateway.webhook_verify import constant_time_equal
from core.memory.working import get_redis
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope

log = get_logger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

INBOUND_STREAM = "agent_core:inbound"


@router.get("/whatsapp")
async def verify_webhook(
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
) -> Response:
    """Handshake de verificación de Meta (endpoint genérico por phone_number_id)."""
    settings = get_settings()
    creds = await effective_credentials()
    verify_token = creds.get("verify_token") or settings.whatsapp_verify_token
    if hub_mode == "subscribe" and verify_token and constant_time_equal(hub_verify_token, verify_token):
        return Response(content=hub_challenge, media_type="text/plain; charset=utf-8")
    return Response(status_code=403)


@router.get("/whatsapp/{slug}")
async def verify_webhook_tenant(
    slug: str,
    hub_mode: str = Query(default="", alias="hub.mode"),
    hub_verify_token: str = Query(default="", alias="hub.verify_token"),
    hub_challenge: str = Query(default="", alias="hub.challenge"),
) -> Response:
    """Handshake de verificación por tenant: `/webhooks/whatsapp/<slug>`.

    Cada app/tenant tiene su propio webhook (la URL la imprime
    `agent-core tenant webhook <slug>`). El verify_token se toma de la conexión
    del tenant (o del `.env`).
    """
    from core.tenants import resolve_tenant_by_slug

    settings = get_settings()
    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        return Response(status_code=403)
    async with tenant_scope(tenant_id):
        creds = await effective_credentials()
    verify_token = creds.get("verify_token") or settings.whatsapp_verify_token
    if hub_mode == "subscribe" and verify_token and constant_time_equal(hub_verify_token, verify_token):
        return Response(content=hub_challenge, media_type="text/plain; charset=utf-8")
    return Response(status_code=403)


@router.post("/whatsapp")
async def receive_whatsapp(request: Request) -> Response:
    """Endpoint genérico: una sola App de Meta compartida (`.env`), puede
    recibir entries de VARIOS tenants en un solo POST si comparten esa App
    -- se resuelve el tenant de CADA entry por separado, nunca uno solo
    para todo el payload."""
    raw_body = await request.body()
    settings = get_settings()
    app_secret = settings.whatsapp_app_secret or settings.meta_app_secret
    headers = {k.lower(): v for k, v in request.headers.items()}

    if app_secret:
        from core.gateway.webhook_verify import verify_signature

        if not verify_signature(raw_body, headers.get("x-hub-signature-256", ""), app_secret):
            log.warning("webhook_signature_invalid")
            await _alert_on_invalid_signature(settings.meta_app_id)
            return Response(status_code=401)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        log.warning("webhook_payload_no_json")
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")

    return await _process_payload(payload, known_tenant_id=None, dev_fallback=True)


async def _alert_on_invalid_signature(meta_app_id: str) -> None:
    """Más de 5 firmas inválidas en 5 min -- alerta EXACTAMENTE una vez al
    cruzar el umbral (no una por cada intento extra)."""
    from core.gateway.webhook_verify import _INVALID_SIG_ALERT_THRESHOLD, record_invalid_signature
    from core.shared.alerts import send_alert

    count = await record_invalid_signature(meta_app_id or "default")
    if count == _INVALID_SIG_ALERT_THRESHOLD + 1:
        await send_alert(
            "Firmas de webhook inválidas repetidas",
            f"Más de {_INVALID_SIG_ALERT_THRESHOLD} intentos con firma inválida en 5 min para "
            f"meta_app_id={meta_app_id} -- App Secret rotado o intento de spoofing.",
            severity="critical",
        )


@router.post("/whatsapp/{slug}")
async def receive_whatsapp_tenant(request: Request, slug: str) -> Response:
    """Webhook por tenant: `/webhooks/whatsapp/<slug>`. El slug identifica al
    tenant -- un solo tenant posible por diseño (una app dedicada = un slug),
    a diferencia del genérico."""
    from core.tenants import is_tenant_active, resolve_tenant_by_slug

    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        log.warning("webhook_tenant_not_found", slug=slug)
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")
    if not await is_tenant_active(tenant_id):
        log.warning("webhook_tenant_not_active", tenant_id=str(tenant_id), slug=slug)
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")

    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    # tenant_scope solo envuelve la verificación de firma, que necesita el
    # app_secret propio del tenant (effective_credentials). El parseo/enrutado
    # de abajo vuelve a abrir su propio tenant_scope por entry en
    # _process_payload -- no muevas lógica sensible fuera de ese `async with`
    # de más abajo asumiendo que este de acá arriba todavía la cubre.
    async with tenant_scope(tenant_id):
        settings = get_settings()
        creds = await effective_credentials()
        app_secret = creds.get("app_secret") or settings.whatsapp_app_secret
        if app_secret:
            from core.gateway.webhook_verify import verify_signature

            if not verify_signature(raw_body, headers.get("x-hub-signature-256", ""), app_secret):
                log.warning("webhook_signature_invalid", tenant_id=str(tenant_id))
                await _alert_on_invalid_signature(creds.get("app_id") or settings.meta_app_id)
                return Response(status_code=401)

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        log.warning("webhook_payload_no_json", tenant_id=str(tenant_id))
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")

    return await _process_payload(payload, known_tenant_id=tenant_id)


async def _process_payload(
    payload: dict[str, Any], *, known_tenant_id: uuid.UUID | None, dev_fallback: bool = False
) -> Response:
    """Aplana, rutea (o usa `known_tenant_id` si ya se sabe por la URL),
    registra en `webhook_events` y encola. Agrupa eventos por `entry_id`:
    todos los eventos de un mismo entry comparten tenant (una entry = una
    WABA/página/cuenta de IG = un tenant).

    `dev_fallback`: solo el endpoint GENÉRICO en APP_ENV=development cae al
    `default_tenant()` si no resuelve nada (bootstrap sin conexión real
    registrada) -- comportamiento preexistente, nunca en producción."""
    from core.tenants import is_tenant_active

    events = flatten_payload(payload)
    settings = get_settings()
    meta_app_id = settings.meta_app_id or "unknown"

    events_by_entry: dict[str, list[FlattenedEvent]] = {}
    for event in events:
        events_by_entry.setdefault(event.entry_id, []).append(event)

    total_enqueued = 0
    total_duplicates = 0
    any_entry_processed = False

    for entry_id, entry_events in events_by_entry.items():
        resolved_tenant_id = await resolve_tenant_for_event(entry_events[0])

        if known_tenant_id is not None and resolved_tenant_id is not None and resolved_tenant_id != known_tenant_id:
            # Defensa en profundidad (hallazgo auditor-multitenant, 2026-09-20):
            # el endpoint por slug NO debe confiar ciegamente en `known_tenant_id`
            # para CUALQUIER entry_id/phone_number_id del payload -- alguien que
            # conozca el App Secret de SU propio tenant podría fabricar un
            # `phone_number_id` real de OTRO tenant para contaminar datos (crear
            # un Customer/Conversation ajeno atribuido a su propio tenant). Si el
            # phone_number_id resuelve a un tenant DISTINTO del de la URL, se
            # rechaza -- nunca se atribuye ciegamente al de la URL.
            log.warning(
                "webhook_tenant_mismatch",
                slug_tenant_id=str(known_tenant_id),
                resolved_tenant_id=str(resolved_tenant_id),
                entry_id=entry_id,
            )
            for event in entry_events:
                ok = await _log_webhook_event(
                    meta_app_id, event, tenant_id=None, process_error="tenant_mismatch"
                )
                if not ok:
                    return _webhook_events_failure_response()
            continue

        tenant_id = known_tenant_id if known_tenant_id is not None else resolved_tenant_id

        if tenant_id is None and dev_fallback and settings.app_env == "development":
            from core.shared.tenant import default_tenant

            tenant_id = await default_tenant()

        if tenant_id is None:
            for event in entry_events:
                ok = await _log_webhook_event(
                    meta_app_id, event, tenant_id=None, process_error="unresolved_tenant"
                )
                if not ok:
                    return _webhook_events_failure_response()
            log.warning("webhook_unresolved_tenant", object_type=entry_events[0].object_type, entry_id=entry_id)
            continue

        if not await is_tenant_active(tenant_id):
            log.warning("webhook_tenant_not_active", tenant_id=str(tenant_id))
            for event in entry_events:
                ok = await _log_webhook_event(
                    meta_app_id, event, tenant_id=tenant_id, process_error="tenant_not_active"
                )
                if not ok:
                    return _webhook_events_failure_response()
            continue

        any_entry_processed = True
        async with tenant_scope(tenant_id):
            for event in entry_events:
                ok = await _log_webhook_event(meta_app_id, event, tenant_id=tenant_id, process_error=None)
                if not ok:
                    return _webhook_events_failure_response()

            enqueued, duplicates = await _dispatch_entry_messages(tenant_id, entry_id, entry_events)
            total_enqueued += enqueued
            total_duplicates += duplicates

            await _dispatch_template_events(entry_events)
            await _dispatch_status_events(entry_events)

            # Marca processed_at DESPUÉS de que los tres dispatch terminaron
            # sin excepción -- es la señal que usa el check de "worker
            # caído" del cron de salud (`count_stalled_webhook_events`).
            # Antes de este ticket, `processed_at` no lo escribía nadie.
            await _mark_entry_processed(meta_app_id, entry_events)

    if not any_entry_processed:
        # Todos los entries se saltaron (sin tenant resuelto o inactivo) --
        # mismo contrato que el código anterior: "ignored", no "ok" con 0.
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")

    body = json.dumps({"status": "ok", "enqueued": total_enqueued, "duplicates": total_duplicates})
    return Response(status_code=200, content=body, media_type="application/json")


def _webhook_events_failure_response() -> Response:
    # 500 A PROPÓSITO (no 200): la única red de seguridad si webhook_events
    # no se pudo escribir es que Meta reintente. Un 500 persistente puede
    # llevar a Meta a deshabilitar el webhook -- la alerta no es opcional
    # (única vía real en este repo hoy: log.error, no hay Sentry/Slack).
    log.error("webhook_events_insert_failed_alertar")
    return Response(status_code=500)


async def _log_webhook_event(
    meta_app_id: str,
    event: FlattenedEvent,
    *,
    tenant_id: uuid.UUID | None,
    process_error: str | None,
) -> bool:
    from datetime import UTC, datetime

    from core.db.models import WebhookEvent
    from core.db.session import get_session

    try:
        async with get_session() as session:
            stmt = pg_insert(WebhookEvent).values(
                tenant_id=tenant_id,
                connection_id=None,
                meta_app_id=meta_app_id,
                object_type=event.object_type,
                field=event.field,
                external_id=event.external_id,
                signature_ok=True,
                payload=event.value,
                received_at=datetime.now(UTC),
                process_error=process_error,
            )
            stmt = stmt.on_conflict_do_nothing(constraint="uq_webhook_events_app_external")
            await session.execute(stmt)
        return True
    except Exception:
        log.exception("webhook_events_insert_error", meta_app_id=meta_app_id, external_id=event.external_id)
        return False


async def _mark_entry_processed(meta_app_id: str, entry_events: list[FlattenedEvent]) -> None:
    """Best-effort: si esto falla, el evento queda `processed_at is null`
    para siempre -- aceptable (el check de "worker caído" lo detectaría
    igual, y `_log_webhook_event` ya es la única red de seguridad real de
    ADR-0028), nunca se re-lanza para no tumbar la respuesta 200 al
    webhook por un fallo puramente de esta marca secundaria."""
    from datetime import UTC, datetime

    from sqlalchemy import update

    from core.db.models import WebhookEvent
    from core.db.session import get_session

    try:
        async with get_session() as session:
            await session.execute(
                update(WebhookEvent)
                .where(
                    WebhookEvent.meta_app_id == meta_app_id,
                    WebhookEvent.external_id.in_([event.external_id for event in entry_events]),
                )
                .values(processed_at=datetime.now(UTC))
            )
    except Exception:
        log.exception("webhook_events_mark_processed_fallo", meta_app_id=meta_app_id)


async def _dispatch_entry_messages(
    tenant_id: uuid.UUID, entry_id: str, entry_events: list[FlattenedEvent]
) -> tuple[int, int]:
    """Reconstruye un mini-payload de UN solo entry y reusa
    `adapter.parse_inbound` (misma lógica de parseo de mensajes que ya
    existía, ahora aplicada por-entry en vez de por-payload completo)."""
    if not entry_events:
        return 0, 0

    object_type = entry_events[0].object_type
    adapter = inbound_adapter_for("meta")

    if object_type == "whatsapp_business_account":
        changes = [{"field": event.field, "value": event.value} for event in entry_events]
        mini_payload = {"object": object_type, "entry": [{"id": entry_id, "changes": changes}]}
    elif object_type in ("page", "instagram"):
        messaging = [event.value for event in entry_events]
        mini_payload = {"object": object_type, "entry": [{"id": entry_id, "messaging": messaging}]}
    else:
        return 0, 0

    messages = adapter.parse_inbound(mini_payload)
    enqueued = 0
    duplicates = 0
    redis = get_redis()

    for message in messages:
        if not await claim_message_once(tenant_id, message.provider, message.external_message_id):
            duplicates += 1
            continue

        if message.media:
            message = await _try_process_media(message)

        await redis.xadd(
            INBOUND_STREAM,
            {
                "tenant_id": str(tenant_id),
                "provider": message.provider,
                "channel": message.channel,
                "external_message_id": message.external_message_id,
                "customer_external_id": message.customer_external_id,
                "customer_name": message.customer_name or "",
                "content": message.content,
                "connection_external_id": message.connection_external_id,
                "media": json.dumps(message.media),
                "media_context": message.media_context,
            },
        )
        enqueued += 1

    return enqueued, duplicates


_TEMPLATE_WEBHOOK_FIELDS = {
    "message_template_status_update", "template_category_update", "message_template_quality_update",
}


async def _dispatch_template_events(entry_events: list[FlattenedEvent]) -> None:
    """Máquina de estados de plantillas -- vive en un módulo aparte
    (`core/gateway/meta_template_webhooks.py`), NUNCA toca `WebhookEvent`
    (ya se logueó en el loop de `_process_payload` antes de llegar acá,
    ADR-0028 intacto)."""
    from core.gateway.meta_template_webhooks import (
        handle_template_category_update,
        handle_template_quality_update,
        handle_template_status_update,
    )

    handlers = {
        "message_template_status_update": handle_template_status_update,
        "template_category_update": handle_template_category_update,
        "message_template_quality_update": handle_template_quality_update,
    }
    for event in entry_events:
        if event.field not in _TEMPLATE_WEBHOOK_FIELDS:
            continue
        try:
            await handlers[event.field](event.value)
        except Exception:
            log.exception("template_webhook_handler_fallo", field=event.field)


async def _dispatch_status_events(entry_events: list[FlattenedEvent]) -> None:
    """`statuses` (sent/delivered/read/failed) de WhatsApp -- NUNCA se
    encolan al stream de inbound (no son mensajes de cliente, no deben
    pasar por el LLM). Cada `FlattenedEvent` de tipo status trae el
    `value` COMPLETO del `change` (puede incluir varios `statuses`); se
    procesa solo el status cuyo `external_id` coincide con el de ESTE
    evento puntual, para no reaplicar el resto N veces."""
    from core.gateway.meta_message_status import apply_status_update

    for event in entry_events:
        if event.object_type != "whatsapp_business_account":
            continue
        phone_number_id = (event.value or {}).get("metadata", {}).get("phone_number_id")
        for status in (event.value or {}).get("statuses", []):
            candidate_id = f"{status.get('id', '')}:{status.get('status', '')}"
            if candidate_id != event.external_id:
                continue
            try:
                await apply_status_update(status, phone_number_id=phone_number_id)
            except Exception:
                log.exception("status_update_handler_fallo", external_id=event.external_id)


async def count_stalled_webhook_events(older_than_minutes: int = 5) -> int:
    """`webhook_events` con `processed_at is null` y `received_at` más
    viejo que `older_than_minutes` -- señal de "worker caído" (el cron de
    salud, `core/tasks/meta_health_check.py`, la chequea cada hora y
    alerta si es > 0). Vive acá porque ADR-0028 exige que solo este módulo
    toque esa tabla."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from core.db.models import WebhookEvent
    from core.db.session import get_session

    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    async with get_session() as session:
        return (
            await session.execute(
                select(func.count(WebhookEvent.id)).where(
                    WebhookEvent.processed_at.is_(None), WebhookEvent.received_at < cutoff
                )
            )
        ).scalar() or 0


async def _try_process_media(message: Any) -> Any:
    """Si mod-media-processor está activo, transforma audio/imagen a texto ANTES de encolar.

    Llamada de función Python en el mismo proceso — cero red (docs/MONOLITH-MODEL.md).
    """
    from core.plugin_manager import plugin_manager

    module = plugin_manager.get("mod_media_processor")
    if module is None or not plugin_manager.is_active("mod_media_processor"):
        if not message.content:
            message.content = "[El cliente envió un archivo multimedia que no puedo procesar]"
        return message
    try:
        processed = await module.process_media(message)  # type: ignore[attr-defined]
        return processed
    except Exception as exc:
        log.exception("media_processing_failed")
        # Acotado al tenant de este webhook (ADR-0024): no apaga el
        # procesador de medios para los demás tenants.
        await plugin_manager.auto_disable_for_tenant(get_current_tenant_id(), "mod_media_processor", str(exc))
        return message
