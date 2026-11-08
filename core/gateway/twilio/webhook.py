"""Webhook de WhatsApp vía Twilio (ADR-0186 §5-7, PR-4): mensajes entrantes y status callbacks.

`POST /webhooks/twilio/{public_id}/inbound` y `/status`, públicas (`/webhooks` no pide sesión). El cuerpo es
`application/x-www-form-urlencoded`. Pasos:

1. Cuerpo crudo con tope (64 KiB): un webhook de Twilio pesa unos pocos KiB.
2. El negocio sale SOLO de la ruta: `twilio_route_connection(public_id)` (SECURITY DEFINER) devuelve
   `(tenant_id, channel_connection_id)`; nada del payload decide el negocio. Un `public_id` desconocido es 404.
3. Dentro del `tenant_scope`: la firma `X-Twilio-Signature` se verifica con el Auth Token de la conexión contra la
   URL EXACTA registrada en el sender (guardada en `twilio_connections`), no contra la que reconstruye el proxy. El
   `AccountSid` del formulario tiene que ser el de la conexión. Falla: 403.
4. Negocio no operativo (ciclo de vida o billing): 200 sin procesar, para que Twilio no reintente. Conexión pausada
   (`active = false`): los entrantes se ignoran con 200, pero los estados de entrega se siguen aplicando (son de
   mensajes que ya salieron). Los cortes son la pausa y la suspensión del negocio; la bandera `whatsapp_twilio` solo
   abre el tipo en el catálogo.
5. Auditoría en la tabla de webhooks (`twilio:<AccountSid>` + `MessageSid` o `MessageSid:MessageStatus`; solo campos
   de diagnóstico, nunca el texto del mensaje). No deduplica: la idempotencia son `claim_message_once` (Redis, 48 h),
   `ProcessedEvent` del consumer y el UNIQUE de `messages`, todos por negocio. La conexión validada viaja en el
   stream (`connection_id`): el consumer no la vuelve a buscar por número.
6. Entrante: `publish_inbound_message` (el mismo camino que Meta). Estado: `apply_status_update` con el dict que
   arma `parse_status` (la misma máquina de estados que Meta; un estado nunca retrocede).

Responde `<Response/>` (TwiML vacío) con `text/xml`: un 200 con otro tipo de contenido deja un aviso en Twilio.
"""

from __future__ import annotations

import uuid
from typing import Any

from agentcore_channel_twilio.adapters import TwilioInbound, form_params
from agentcore_channel_twilio.status import parse_status
from fastapi import APIRouter, Request, Response
from sqlalchemy import select, text

from core.db.models import ChannelConnection, TwilioConnection
from core.db.session import get_session
from core.gateway.webhook_flatten import FlattenedEvent
from core.shared.crypto import get_secret_box
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

router = APIRouter(prefix="/webhooks/twilio", tags=["webhooks"])

MAX_BODY_BYTES = 64 * 1024
# Directo, no por el registry: el webhook es de este proveedor y no depende de que el entry point esté instalado.
_ADAPTADOR = TwilioInbound()
_TWIML_VACIO = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
# Campos del formulario que se guardan en la auditoría: sirven para diagnosticar sin guardar el mensaje.
_CAMPOS_DIAGNOSTICO = (
    "MessageSid",
    "AccountSid",
    "MessageStatus",
    "SmsStatus",
    "ErrorCode",
    "NumMedia",
    "MessageType",
    "ChannelPrefix",
)


async def _cuerpo_con_tope(request: Request) -> bytes | None:
    """Cuerpo crudo leído por partes con tope (no confía en `Content-Length`); `None` si lo supera."""
    declarado = request.headers.get("content-length")
    if (
        declarado is not None
        and declarado.isascii()
        and declarado.isdigit()
        and int(declarado) > MAX_BODY_BYTES
    ):
        return None
    cuerpo = bytearray()
    async for chunk in request.stream():
        cuerpo.extend(chunk)
        if len(cuerpo) > MAX_BODY_BYTES:
            return None
    return bytes(cuerpo)


def _twiml(status_code: int = 200) -> Response:
    return Response(content=_TWIML_VACIO, media_type="text/xml", status_code=status_code)


async def _ruta(public_id: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    if not public_id or len(public_id) > 64:
        return None
    async with get_session() as session:
        fila = (
            await session.execute(
                text("select tenant_id, channel_connection_id from twilio_route_connection(:p)"),
                {"p": public_id},
            )
        ).first()
    return (fila[0], fila[1]) if fila is not None else None


async def _conexion(
    tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> tuple[ChannelConnection, TwilioConnection] | None:
    async with get_session() as session:
        fila = (
            await session.execute(
                select(ChannelConnection, TwilioConnection)
                .join(
                    TwilioConnection,
                    (TwilioConnection.channel_connection_id == ChannelConnection.id)
                    & (TwilioConnection.tenant_id == ChannelConnection.tenant_id),
                )
                .where(
                    ChannelConnection.id == connection_id,
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.provider == "twilio",
                    ChannelConnection.deleted_at.is_(None),
                    TwilioConnection.deleted_at.is_(None),
                )
            )
        ).first()
    return (fila[0], fila[1]) if fila is not None else None


def _evento(kind: str, external_id: str, public_id: str, params: dict[str, str]) -> FlattenedEvent:
    return FlattenedEvent(
        object_type="twilio",
        field=kind,
        external_id=external_id[:200],
        entry_id=public_id,
        sub_id=None,
        value={k: params[k] for k in _CAMPOS_DIAGNOSTICO if params.get(k)},
    )


async def _recibir(request: Request, public_id: str, kind: str) -> Response:
    from core.billing.enforcement import is_tenant_operational
    from core.gateway.webhooks import (
        _alert_on_invalid_signature,
        _log_webhook_event,
        _mark_entry_processed,
        publish_inbound_message,
    )

    raw_body = await _cuerpo_con_tope(request)
    if raw_body is None:
        return Response(status_code=413)
    ruta = await _ruta(public_id)
    if ruta is None:
        log.info("twilio_webhook_public_id_desconocido", kind=kind)
        return Response(status_code=404)
    tenant_id, connection_id = ruta

    async with tenant_scope(tenant_id):
        par = await _conexion(tenant_id, connection_id)
        if par is None:
            return Response(status_code=404)
        connection, twilio = par
        creds: dict[str, Any] = (
            get_secret_box().decrypt_json(connection.credentials_encrypted)
            if connection.credentials_encrypted
            else {}
        )
        url = twilio.webhook_inbound_url if kind == "inbound" else twilio.webhook_status_url
        adapter = _ADAPTADOR
        headers = {k.lower(): v for k, v in request.headers.items()}
        params = dict(form_params(raw_body))
        firma_ok = adapter.verify_request(
            raw_body, headers, str(creds.get("auth_token") or ""), url=url
        )
        if not firma_ok or params.get("AccountSid") != twilio.account_sid:
            log.warning(
                "twilio_webhook_signature_invalid",
                tenant_id=str(tenant_id),
                connection_id=str(connection_id),
                kind=kind,
                account_ok=params.get("AccountSid") == twilio.account_sid,
            )
            await _alert_on_invalid_signature(f"twilio:{twilio.account_sid}")
            return Response(status_code=403)

        if not connection.active and kind == "inbound":
            log.info("twilio_webhook_conexion_pausada", tenant_id=str(tenant_id), kind=kind)
            return _twiml()
        if not await is_tenant_operational(tenant_id):
            log.info("twilio_webhook_negocio_no_operativo", tenant_id=str(tenant_id), kind=kind)
            return _twiml()

        message_sid = str(params.get("MessageSid") or "")
        if not message_sid:
            return _twiml()
        app_id = f"twilio:{twilio.account_sid}"
        if kind == "inbound":
            evento = _evento(kind, message_sid, public_id, params)
        else:
            update = parse_status(params)
            if update is None:
                log.info(
                    "twilio_status_desconocido",
                    tenant_id=str(tenant_id),
                    status=params.get("MessageStatus"),
                )
                return _twiml()
            evento = _evento(kind, f"{message_sid}:{update['provider_status']}", public_id, params)

        if not await _log_webhook_event(app_id, evento, tenant_id=tenant_id, process_error=None):
            # Sin la capa 1 no se procesa: 500 para que Twilio pueda reintentar (si su reintento aplica).
            log.error("twilio_webhook_auditoria_fallida", tenant_id=str(tenant_id), kind=kind)
            return _twiml(500)

        if kind == "inbound":
            resultado = "ignored"
            for message in adapter.parse_inbound(params):
                if message.connection_external_id != twilio.sender_e164:
                    # El `To` firmado tiene que ser el número de ESTA conexión: con otro, la conversación nacería sin
                    # conexión y su respuesta podría salir por la primaria o por el número de la plataforma.
                    log.warning(
                        "twilio_webhook_to_ajeno",
                        tenant_id=str(tenant_id),
                        connection_id=str(connection_id),
                    )
                    resultado = "ignored"
                    continue
                resultado = await publish_inbound_message(
                    tenant_id, message, connection_id=connection_id
                )
            log.info(
                "twilio_webhook_received", tenant_id=str(tenant_id), kind=kind, result=resultado
            )
        else:
            from core.gateway.meta_message_status import apply_status_update

            try:
                await apply_status_update(
                    update, expected_tenant_id=tenant_id, phone_number_id=None
                )
            except Exception:
                log.exception("twilio_status_update_failed", tenant_id=str(tenant_id))
            from core.gateway.twilio.costos import ESTADOS_FINALES

            if update["provider_status"] in ESTADOS_FINALES:
                # El costo se lee aparte (el precio tarda): no demora la respuesta a Twilio.
                from core.tasks.twilio_costos import encolar_costo

                encolar_costo(tenant_id, connection_id, message_sid)
            log.info(
                "twilio_webhook_received",
                tenant_id=str(tenant_id),
                kind=kind,
                status=update["status"],
            )
        await _mark_entry_processed(app_id, tenant_id, [evento])
    return _twiml()


@router.post("/{public_id}/inbound", include_in_schema=False)
async def recibir_entrante(request: Request, public_id: str) -> Response:
    return await _recibir(request, public_id, "inbound")


@router.post("/{public_id}/status", include_in_schema=False)
async def recibir_estado(request: Request, public_id: str) -> Response:
    return await _recibir(request, public_id, "status")
