"""Endpoints de webhooks de canales.

Flujo (patrón gateway -> queue -> consumer de agent-sale):
1. Aplanar el payload en eventos individuales (`webhook_flatten.py`) y
   resolver el tenant de cada grupo (entry, número) (`webhook_route_tenant.py`)
   -- un payload puede traer entries de varios tenants si comparten la misma
   App de Meta.
2. Verificar la firma (HMAC-SHA256 con App Secret) y saber QUIÉN firmó: la
   plataforma autoriza a cualquier negocio; un negocio, solo a sí mismo. Un
   grupo de otro negocio no se procesa (ADR-0100).
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
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.config import get_settings
from core.gateway.channels.registry import inbound_adapter_for
from core.gateway.connections import effective_credentials
from core.gateway.idempotency import claim_message_once
from core.gateway.webhook_flatten import FlattenedEvent, flatten_payload
from core.gateway.webhook_route_tenant import EventRoute, route_event
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
    from core.gateway.connections import resolve_effective_verify_token
    from core.tenants import resolve_tenant_by_slug

    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        return Response(status_code=403)
    # Orden: ChannelConnection (conexión manual) -> MetaConnection BYOK
    # (verify_token propio generado en el wizard) -> `.env` (global). Misma
    # resolución que usa el registro (`/admin/conexiones`), para que el token
    # registrado en Meta sea el que este handler acepta.
    async with tenant_scope(tenant_id):
        verify_token = await resolve_effective_verify_token(tenant_id)
    if hub_mode == "subscribe" and verify_token and constant_time_equal(hub_verify_token, verify_token):
        return Response(content=hub_challenge, media_type="text/plain; charset=utf-8")
    return Response(status_code=403)


@dataclass(frozen=True)
class _Grupo:
    """Eventos de una entry y un número, ya ruteados (ADR-0095). Se rutea una sola vez
    por POST: la firma y el despacho usan la misma resolución."""

    entry_id: str
    sub_id: str | None
    events: list[FlattenedEvent]
    route: EventRoute


@dataclass(frozen=True)
class Firmantes:
    """Quién firmó el POST (ADR-0100). La app de la plataforma autoriza eventos de
    cualquier negocio (ningún negocio conoce su secreto); el secreto propio de un
    negocio, solo los suyos. Con un secreto propio, un negocio podía firmar eventos
    con el número de otro e inyectarle mensajes."""

    plataforma: bool
    negocios: frozenset[uuid.UUID] = frozenset()

    def autoriza(self, tenant_id: uuid.UUID) -> bool:
        return self.plataforma or tenant_id in self.negocios

    @classmethod
    def de_negocio(cls, tenant_id: uuid.UUID) -> Firmantes:
        return cls(plataforma=False, negocios=frozenset({tenant_id}))


async def _rutear_grupos(payload: dict[str, Any]) -> list[_Grupo]:
    """Agrupa por (entry, número), no por entry: con el ruteo por número primero
    (ADR-0095), dos números de una misma entry pueden ser de negocios distintos."""
    por_grupo: dict[tuple[str, str | None], list[FlattenedEvent]] = {}
    for event in flatten_payload(payload):
        por_grupo.setdefault((event.entry_id, event.sub_id), []).append(event)
    return [
        _Grupo(entry_id, sub_id, events, await route_event(events[0]))
        for (entry_id, sub_id), events in por_grupo.items()
    ]


async def _secretos_propios(tenant_id: uuid.UUID) -> tuple[str, ...] | None:
    """Secretos propios del negocio (conexión manual o BYOK), sin el de la plataforma.
    El BYOK vive bajo RLS de tenant (custodia `meta_token_store`): se resuelve dentro de
    `tenant_scope` o la lectura no lo ve (bug real, 2026-09-29). Una tupla: con
    HU-CONN-05 un negocio podrá tener un secreto por conexión. `None` si no se pudo leer."""
    from core.gateway.connections import resolve_own_app_secret

    try:
        async with tenant_scope(tenant_id):
            propio = await resolve_own_app_secret(tenant_id)
    except Exception:
        log.exception("webhook_tenant_secret_failed", tenant_id=str(tenant_id))
        return None
    return (propio,) if propio else ()


async def _firmantes_del_post(raw_body: bytes, firma: str, grupos: list[_Grupo]) -> Firmantes | None:
    """Quién firmó el POST, o `None` si no hay ningún secreto contra el que verificar.

    Primero la plataforma: si firmó, no hace falta mirar los secretos de los negocios.
    Si no, se prueba el secreto propio de cada negocio de los grupos (los que están en
    conflicto no aportan candidato). `Firmantes` sin plataforma ni negocios = firma
    inválida."""
    from core.gateway.connections import platform_app_secret
    from core.gateway.webhook_verify import verify_signature

    plataforma = platform_app_secret()
    if plataforma and verify_signature(raw_body, firma, plataforma):
        return Firmantes(plataforma=True)

    candidatos = dict.fromkeys(
        g.route.tenant_id for g in grupos if g.route.tenant_id is not None and not g.route.conflict
    )
    hay_secreto = bool(plataforma)
    negocios: set[uuid.UUID] = set()
    for tenant_id in candidatos:
        secretos = await _secretos_propios(tenant_id)
        if secretos is None:
            # No se pudo leer: puede que tenga secreto. Nunca habilita el escape sin firma.
            hay_secreto = True
            continue
        for secreto in secretos:
            hay_secreto = True
            if verify_signature(raw_body, firma, secreto):
                negocios.add(tenant_id)
                break
    if not hay_secreto:
        return None
    if len(negocios) > 1:
        # Dos negocios con la misma app: se autorizan entre sí y no se puede distinguir.
        log.warning("webhook_secreto_compartido", tenant_ids=sorted(str(t) for t in negocios))
    return Firmantes(plataforma=False, negocios=frozenset(negocios))


@router.post("/whatsapp")
async def receive_whatsapp(request: Request) -> Response:
    """Endpoint genérico: puede recibir entries de VARIOS tenants en un solo POST
    (todos los que estén suscritos a la misma App de Meta). Se rutea cada grupo
    (entry, número) por separado, y cada uno se procesa solo si lo firmó la
    plataforma o su propio negocio (ADR-0100)."""
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        log.warning("webhook_payload_no_json")
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")
    if not isinstance(payload, dict):
        log.warning("webhook_payload_no_es_objeto")
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")

    grupos = await _rutear_grupos(payload)
    firmantes = await _firmantes_del_post(raw_body, headers.get("x-hub-signature-256", ""), grupos)
    if firmantes is None:
        # Sin ningún secreto resoluble, fail-closed (HU-ARCH-05): no se procesa
        # un mensaje sin firma verificable salvo el escape de test.
        primero = next((g.route.tenant_id for g in grupos if g.route.tenant_id is not None), None)
        rechazo = await _fail_closed_sin_secreto(tenant_id=primero)
        if rechazo is not None:
            return rechazo
        firmantes = Firmantes(plataforma=True)  # escape de test: nada que verificar, como antes
    elif not firmantes.plataforma and not firmantes.negocios:
        log.warning("webhook_signature_invalid", grupos=len(grupos))
        await _alert_on_invalid_signature(get_settings().meta_app_id)
        return Response(status_code=401)
    elif not firmantes.plataforma:
        # Una app propia apuntando al genérico (el wizard BYOK registra el slug): se mide
        # para poder migrar esas suscripciones y retirar esta rama (ADR-0100).
        log.info("webhook_generico_firmado_por_negocio", tenant_ids=sorted(str(t) for t in firmantes.negocios))

    return await _process_payload(
        payload, known_tenant_id=None, dev_fallback=True, firmantes=firmantes, grupos=grupos
    )


_FIRMA_AJENA_VENTANA_S = 24 * 60 * 60


async def _alert_on_firma_ajena(firmantes: Firmantes, ajenos: list[uuid.UUID]) -> None:
    """Un negocio firmó eventos de otro: intento de inyección, o la WABA de un negocio
    suscrita a la app de otro. Alerta una vez al día por par firmante-afectados: una
    configuración rota alertaría cada pocos minutos y terminaría silenciando el canal.
    Nunca corta el webhook: un fallo acá se loguea y se sigue."""
    from core.shared.alerts import send_alert

    firmantes_ids = ",".join(sorted(str(t) for t in firmantes.negocios))
    ajenos_ids = ",".join(sorted({str(t) for t in ajenos}))
    try:
        primera_vez = await get_redis().set(
            f"webhook_firma_ajena:{firmantes_ids}:{ajenos_ids}", "1", nx=True, ex=_FIRMA_AJENA_VENTANA_S
        )
    except Exception:
        # Sin Redis no hay dedupe: mejor alertar de más que callar un intento de inyección.
        log.exception("webhook_alerta_firma_ajena_sin_redis")
        primera_vez = True
    if not primera_vez:
        return
    try:
        await send_alert(
            "Un negocio firmó eventos de otro",
            f"Llegaron eventos de {ajenos_ids} firmados con el secreto de {firmantes_ids}: no se "
            "procesaron. Puede ser un intento de inyección o una WABA suscrita a la app de otro "
            "negocio (ADR-0100).",
            severity="critical",
        )
    except Exception:
        log.exception("webhook_alerta_firma_ajena_fallo")


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


def _unsigned_allowed() -> bool:
    """`WEBHOOK_ALLOW_UNSIGNED=true` (solo entornos de prueba) permite procesar
    sin firma, con aviso. En producción nunca se permite (fail-closed)."""
    settings = get_settings()
    if settings.webhook_allow_unsigned:
        if settings.app_env == "production":
            log.error("webhook_allow_unsigned_en_produccion")
            return False
        log.warning("webhook_sin_firma_permitido_por_flag")
        return True
    return False


async def _fail_closed_sin_secreto(*, tenant_id: uuid.UUID | None) -> Response | None:
    """Sin `app_secret` resoluble, fail-closed: 401, no se encola y se alerta.

    Devuelve la respuesta 401 si hay que rechazar; `None` si se puede seguir
    (existe secreto, o el escape de test está activo).
    """
    if _unsigned_allowed():
        return None
    log.error("webhook_sin_secreto_fail_closed", tenant_id=str(tenant_id) if tenant_id else None)
    await _alert_on_invalid_signature(get_settings().meta_app_id or "default")
    return Response(status_code=401)


@router.post("/whatsapp/{slug}")
async def receive_whatsapp_tenant(request: Request, slug: str) -> Response:
    """Webhook por tenant: `/webhooks/whatsapp/<slug>`. El slug identifica al
    tenant -- un solo tenant posible por diseño (una app dedicada = un slug),
    a diferencia del genérico."""
    from core.billing.enforcement import is_tenant_operational
    from core.tenants import resolve_tenant_by_slug

    tenant_id = await resolve_tenant_by_slug(slug)
    if tenant_id is None:
        log.warning("webhook_tenant_not_found", slug=slug)
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")

    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    # tenant_scope solo envuelve la verificación de firma, que necesita el
    # app_secret propio del tenant (effective_credentials). El parseo/enrutado
    # de abajo vuelve a abrir su propio tenant_scope por entry en
    # _process_payload -- no muevas lógica sensible fuera de ese `async with`
    # de más abajo asumiendo que este de acá arriba todavía la cubre.
    async with tenant_scope(tenant_id):
        from core.gateway.connections import resolve_effective_app_secret

        app_secret = await resolve_effective_app_secret(tenant_id)
        if not app_secret:
            # Sin secreto para este tenant, fail-closed (HU-ARCH-05).
            rechazo = await _fail_closed_sin_secreto(tenant_id=tenant_id)
            if rechazo is not None:
                return rechazo
        else:
            from core.gateway.webhook_verify import verify_signature

            if not verify_signature(raw_body, headers.get("x-hub-signature-256", ""), app_secret):
                log.warning("webhook_signature_invalid", tenant_id=str(tenant_id))
                await _alert_on_invalid_signature(get_settings().meta_app_id or str(tenant_id))
                return Response(status_code=401)

    # Gate de billing DESPUÉS de la firma: antes filtraría el estado de
    # facturación de un tenant a quien no está autenticado (401 vs. 200).
    if not await is_tenant_operational(tenant_id):
        log.warning("webhook_tenant_not_operational", tenant_id=str(tenant_id), slug=slug)
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        log.warning("webhook_payload_no_json", tenant_id=str(tenant_id))
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")

    return await _process_payload(payload, known_tenant_id=tenant_id, firmantes=Firmantes.de_negocio(tenant_id))


async def _process_payload(
    payload: dict[str, Any],
    *,
    known_tenant_id: uuid.UUID | None,
    firmantes: Firmantes,
    dev_fallback: bool = False,
    grupos: list[_Grupo] | None = None,
) -> Response:
    """Aplana, rutea (o usa `known_tenant_id` si ya se sabe por la URL),
    registra en `webhook_events` y encola, grupo por grupo (entry, número).

    `firmantes` es obligatorio (falla cerrado por construcción): un grupo solo se
    procesa si su negocio está entre los que firmaron, o si firmó la plataforma
    (ADR-0100). El endpoint por slug pasa su propio tenant. `grupos`: los ya
    ruteados por el llamador, para no rutear dos veces.

    `dev_fallback`: solo el endpoint GENÉRICO en APP_ENV=development cae al
    `default_tenant()` si no resuelve nada (bootstrap sin conexión real
    registrada) -- comportamiento preexistente, nunca en producción; también
    necesita estar autorizado por la firma."""
    from core.billing.enforcement import is_tenant_operational

    if grupos is None:
        grupos = await _rutear_grupos(payload)
    settings = get_settings()
    meta_app_id = settings.meta_app_id or "unknown"

    total_enqueued = 0
    total_duplicates = 0
    any_entry_processed = False
    ajenos: list[uuid.UUID] = []

    for grupo in grupos:
        entry_id, sub_id, entry_events, route = grupo.entry_id, grupo.sub_id, grupo.events, grupo.route
        resolved_tenant_id = route.tenant_id

        if route.conflict:
            # Los activos del evento apuntan a más de un negocio (ADR-0095): no va
            # a ninguno, ni al del slug ni al `default_tenant()` de desarrollo.
            log.warning(
                "webhook_tenant_conflict",
                slug_tenant_id=str(known_tenant_id) if known_tenant_id else None,
                entry_id=entry_id,
                phone_number_id=sub_id,
            )
            for event in entry_events:
                ok = await _log_webhook_event(
                    meta_app_id, event, tenant_id=None, process_error="tenant_conflict"
                )
                if not ok:
                    return _webhook_events_failure_response()
            continue

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
                phone_number_id=sub_id,
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
            log.warning(
                "webhook_unresolved_tenant",
                object_type=entry_events[0].object_type,
                entry_id=entry_id,
                phone_number_id=sub_id,
            )
            continue

        if not firmantes.autoriza(tenant_id):
            # Firmado por otro negocio (ADR-0100): no se procesa ni se le atribuye a
            # nadie. Va antes del chequeo de facturación, que registraría la fila a
            # nombre de este negocio (`tenant_not_operational`) aunque no la mandó.
            log.warning(
                "webhook_firma_no_es_del_negocio",
                tenant_id=str(tenant_id),
                firmantes=sorted(str(t) for t in firmantes.negocios),
                entry_id=entry_id,
                phone_number_id=sub_id,
                eventos=len(entry_events),
            )
            for event in entry_events:
                ok = await _log_webhook_event(
                    meta_app_id, event, tenant_id=None, process_error="signature_tenant_mismatch",
                    signature_ok=False,
                )
                if not ok:
                    return _webhook_events_failure_response()
            ajenos.append(tenant_id)
            continue

        if not await is_tenant_operational(tenant_id):
            log.warning("webhook_tenant_not_operational", tenant_id=str(tenant_id))
            for event in entry_events:
                ok = await _log_webhook_event(
                    meta_app_id, event, tenant_id=tenant_id, process_error="tenant_not_operational"
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

            await _dispatch_template_events(tenant_id, entry_events)
            await _dispatch_status_events(tenant_id, entry_events)

            # Marca processed_at DESPUÉS de que los tres dispatch terminaron
            # sin excepción -- es la señal que usa el check de "worker
            # caído" del cron de salud (`count_stalled_webhook_events`).
            # Antes de este ticket, `processed_at` no lo escribía nadie.
            await _mark_entry_processed(meta_app_id, tenant_id, entry_events)

    if ajenos:
        await _alert_on_firma_ajena(firmantes, ajenos)

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
    signature_ok: bool = True,
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
                signature_ok=signature_ok,
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


async def _mark_entry_processed(
    meta_app_id: str, tenant_id: uuid.UUID, entry_events: list[FlattenedEvent]
) -> None:
    """Best-effort: si esto falla, el evento queda `processed_at is null`
    para siempre -- aceptable (el check de "worker caído" lo detectaría
    igual, y `_log_webhook_event` ya es la única red de seguridad real de
    ADR-0028), nunca se re-lanza para no tumbar la respuesta 200 al
    webhook por un fallo puramente de esta marca secundaria.

    Filtra por el negocio del grupo: `meta_app_id` es el de la plataforma (el
    mismo para todos), y un `external_id` repetido no puede marcar como
    procesada la fila de otro negocio (ADR-0100)."""
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
                    WebhookEvent.tenant_id == tenant_id,
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
            processed = await _try_process_media(message)
            if processed is None:
                # Cuota de audio/OCR agotada: se descarta el mensaje (ADR-0045).
                continue
            message = processed

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


async def _dispatch_template_events(tenant_id: uuid.UUID, entry_events: list[FlattenedEvent]) -> None:
    """Máquina de estados de plantillas -- vive en un módulo aparte
    (`core/gateway/meta_template_webhooks.py`), NUNCA toca `WebhookEvent`
    (ya se logueó en el loop de `_process_payload` antes de llegar acá,
    ADR-0028 intacto). `tenant_id` es el negocio del grupo, ya autorizado por
    la firma: una plantilla de otro negocio no se toca (ADR-0100)."""
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
            await handlers[event.field](event.value, expected_tenant_id=tenant_id)
        except Exception:
            log.exception("template_webhook_handler_fallo", field=event.field)


async def _dispatch_status_events(tenant_id: uuid.UUID, entry_events: list[FlattenedEvent]) -> None:
    """`statuses` (sent/delivered/read/failed) de WhatsApp -- NUNCA se
    encolan al stream de inbound (no son mensajes de cliente, no deben
    pasar por el LLM). Cada `FlattenedEvent` de tipo status trae el
    `value` COMPLETO del `change` (puede incluir varios `statuses`); se
    procesa solo el status cuyo `external_id` coincide con el de ESTE
    evento puntual, para no reaplicar el resto N veces. `tenant_id` es el
    negocio del grupo: un mensaje de otro negocio no se toca (ADR-0100)."""
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
                await apply_status_update(status, expected_tenant_id=tenant_id, phone_number_id=phone_number_id)
            except Exception:
                log.exception("status_update_handler_fallo", external_id=event.external_id)


async def count_stalled_webhook_events(older_than_minutes: int = 5) -> int:
    """`webhook_events` con `processed_at is null` y `received_at` más
    viejo que `older_than_minutes` -- señal de "worker caído" (el cron de
    salud, `core/tasks/meta_health_check.py`, la chequea cada hora y
    alerta si es > 0). Vive acá porque ADR-0028 exige que solo este módulo
    toque esa tabla.

    `webhook_events` es tabla de SISTEMA sin RLS (tenant_id nullable a
    propósito), así que este `select` sin `tenant_scope` ve todos los tenants
    -- no hace falta función SECURITY DEFINER."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from core.db.models import WebhookEvent
    from core.db.session import get_session

    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    async with get_session() as session:
        return (
            await session.execute(
                select(func.count(WebhookEvent.id)).where(
                    WebhookEvent.processed_at.is_(None),
                    WebhookEvent.received_at < cutoff,
                    # Los rechazados (sin negocio, conflicto, firma ajena...) nunca se
                    # marcan procesados: no son un worker caído (ADR-0100).
                    WebhookEvent.process_error.is_(None),
                )
            )
        ).scalar() or 0


def _media_metric(message: Any) -> str | None:
    """Métrica de cuota que consume un mensaje multimedia (audio u OCR)."""
    for item in getattr(message, "media", []) or []:
        mime = str(item.get("type", ""))
        if mime.startswith("audio"):
            return "conversaciones_audio"
        if mime.startswith("image"):
            return "conversaciones_ocr"
    return None


async def _try_process_media(message: Any) -> Any:
    """Si mod-media-processor está activo, transforma audio/imagen a texto ANTES de encolar.

    Devuelve None si la cuota de audio/OCR del plan está agotada: el mensaje se
    descarta (bloqueo duro, ADR-0045) y no se encola.

    Llamada de función Python en el mismo proceso — cero red (docs/MONOLITH-MODEL.md).
    """
    metric = _media_metric(message)
    tenant_id = get_current_tenant_id()
    if metric is not None and tenant_id is not None:
        from core.billing.enforcement import check_metric

        if not await check_metric(tenant_id, metric):
            log.warning("media_quota_exceeded", tenant_id=str(tenant_id), metric=metric)
            return None

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


# ---------------------------------------------------------------- webhook de billing (ADR-0046)
@router.post("/billing/wompi")
async def receive_billing_wompi(request: Request) -> Response:
    """Webhook del pago de la SUSCRIPCIÓN de plataforma (Wompi).

    Punto de entrada de SISTEMA (no de tenant): el pago no trae `tenant_id` en el
    payload, se resuelve por `provider_reference` (id del link) contra
    `subscription_payments`. Verifica la firma con el `events_secret` de la
    plataforma (`.env`), nunca con credenciales de un tenant.
    """
    from core.billing.providers import get_billing_provider

    provider = get_billing_provider()
    if provider is None:
        log.warning("billing_webhook_no_provider")
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")

    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")

    if not provider.verify_webhook_signature(payload):
        log.warning("billing_webhook_signature_invalid")
        return Response(status_code=401)

    event = provider.parse_webhook_event(payload)
    if not event.provider_reference:
        log.warning("billing_webhook_sin_referencia")
        return Response(status_code=200, content=json.dumps({"status": "ignored"}), media_type="application/json")

    from core.billing.checkout import apply_payment, tenant_del_pago

    try:
        tenant_id = await tenant_del_pago(event.provider_reference)
        if tenant_id is None:
            # R-05 (ADR-0123): con la cuenta de Wompi compartida, el evento puede ser de un
            # pago del negocio que vende los planes (su pack de pagos).
            from core.billing.enrutador import reenviar_al_negocio

            destino = await reenviar_al_negocio(payload)
            return Response(
                status_code=200,
                content=json.dumps({"status": "reenviado" if destino != "ignored" else "ignored"}),
                media_type="application/json",
            )
        updated = await apply_payment(
            provider_reference=event.provider_reference, status=event.status, tenant_id=tenant_id
        )
    except Exception:
        log.exception("billing_webhook_apply_failed")
        # 500 a propósito: Wompi reintenta el evento.
        return Response(status_code=500)

    return Response(
        status_code=200,
        content=json.dumps({"status": "ok", "updated": updated, "payment": event.status}),
        media_type="application/json",
    )
