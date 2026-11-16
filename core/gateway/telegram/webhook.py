"""Webhook del canal Telegram (ADR-0199 §4-5): `POST /webhooks/telegram/{public_id}`.

Pública (`/webhooks` no pide sesión). Telegram manda un `Update` en JSON por cada evento y reintenta todo lo que no
sea 2xx (descarta lo pendiente a las 24 h). Pasos:

1. Kill-switch apagado: 503 sin leer nada, para que Telegram reintente cuando se vuelva a encender.
2. Sin el encabezado del secreto, o con una forma que Telegram no manda: 401 sin tocar la base (barato ante abuso).
3. El negocio sale SOLO de la ruta: `telegram_route_connection(public_id)` (SECURITY DEFINER) devuelve
   `(tenant_id, channel_connection_id)`; nada del cuerpo decide el negocio. Un `public_id` desconocido es 404.
4. Dentro del `tenant_scope`: el encabezado `X-Telegram-Bot-Api-Secret-Token` tiene que ser el secreto de ESTA
   conexión (comparación en tiempo constante). Falla: 401 y cuenta para la alerta de firmas inválidas. Recién
   entonces se lee el cuerpo, con tope (64 KiB): un update de texto pesa poco y los medios llegan como `file_id`.
5. JSON inválido o un update que no se procesa (grupo, mensaje editado, sticker…): 200 «ignorado», para no trabar la
   cola de Telegram. Conexión pausada o negocio no operativo: 200 sin procesar.
6. Auditoría en la tabla de webhooks (`telegram:<bot_id>` + `tg:<bot_id>:<update_id>`; solo campos de diagnóstico,
   nunca el texto). Si no se puede auditar: 500 y Telegram reintenta. No deduplica: la idempotencia son
   `claim_message_once` (Redis), `ProcessedEvent` del consumer y el UNIQUE de `messages`, todos con la llave global
   del mensaje (`tg:<bot_id>:in:<chat_id>:<message_id>` o `tg:<bot_id>:cb:<callback_query.id>`).
7. `publish_inbound_message` con la conexión validada (`connection_id` viaja en el stream). Redis caído: 503.
8. Un toque de botón (`callback_query`) se contesta después de publicar (`answerCallbackQuery`, 2 s, best-effort):
   quita el reloj del botón en el Telegram del cliente.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

from agentcore_channel_telegram.adapters import HEADER_SECRETO, TelegramInbound, callback_query_id
from agentcore_channel_telegram.client import TelegramClient
from agentcore_channel_telegram.limites import secreto_webhook_valido
from fastapi import APIRouter, Request, Response
from opentelemetry import metrics
from sqlalchemy import func, select, text, update

from core.db.models import ChannelConnection, TelegramConnection
from core.db.session import get_session
from core.gateway.channels.base import ProviderError
from core.gateway.telegram.habilitado import habilitado
from core.gateway.webhook_flatten import FlattenedEvent
from core.shared.crypto import get_secret_box
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

router = APIRouter(prefix="/webhooks/telegram", tags=["webhooks"])

MAX_BODY_BYTES = 64 * 1024
# Directo, no por el registry: el webhook es de este proveedor y no depende de que el entry point esté instalado.
_ADAPTADOR = TelegramInbound()
_RESPUESTA_CALLBACK_S = 2.0
# Tipos de update que se registran en la auditoría por su nombre (nunca su contenido).
_TIPOS = ("message", "edited_message", "callback_query", "channel_post", "my_chat_member", "chat_member")

# Observabilidad (ADR-0199 §6). Etiquetas cerradas: nunca negocio, bot ni chat.
RESULTADOS = frozenset(
    {
        "aceptado",
        "duplicado",
        "descartado",
        "ignorado",
        "pausado",
        "secreto_invalido",
        "desconocido",
        "grande",
        "apagado",
        "error",
    }
)
_meter = metrics.get_meter("agent-core")
_total = _meter.create_counter(
    "telegram_webhook_total", description="Webhooks de Telegram recibidos, por resultado"
)
_duracion = _meter.create_histogram(
    "telegram_webhook_duracion_seg", unit="s", description="Duración del webhook de Telegram"
)


def _medir(resultado: str, inicio: float) -> None:
    if resultado not in RESULTADOS:
        resultado = "error"
    _total.add(1, {"resultado": resultado})
    _duracion.record(time.monotonic() - inicio, {"resultado": resultado})


async def _cuerpo_con_tope(request: Request) -> bytes | None:
    """Cuerpo crudo leído por partes con tope (no confía en `Content-Length`); `None` si lo supera."""
    declarado = request.headers.get("content-length")
    if declarado is not None and declarado.isascii() and declarado.isdigit() and int(declarado) > MAX_BODY_BYTES:
        return None
    cuerpo = bytearray()
    async for chunk in request.stream():
        cuerpo.extend(chunk)
        if len(cuerpo) > MAX_BODY_BYTES:
            return None
    return bytes(cuerpo)


async def _ruta(public_id: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    if not public_id or len(public_id) > 64:
        return None
    async with get_session() as session:
        fila = (
            await session.execute(
                text("select tenant_id, channel_connection_id from telegram_route_connection(:p)"),
                {"p": public_id},
            )
        ).first()
    return (fila[0], fila[1]) if fila is not None else None


async def _conexion(
    tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> tuple[ChannelConnection, TelegramConnection] | None:
    async with get_session() as session:
        fila = (
            await session.execute(
                select(ChannelConnection, TelegramConnection)
                .join(
                    TelegramConnection,
                    (TelegramConnection.channel_connection_id == ChannelConnection.id)
                    & (TelegramConnection.tenant_id == ChannelConnection.tenant_id),
                )
                .where(
                    ChannelConnection.id == connection_id,
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.provider == "telegram",
                    ChannelConnection.deleted_at.is_(None),
                    TelegramConnection.deleted_at.is_(None),
                )
            )
        ).first()
    return (fila[0], fila[1]) if fila is not None else None


def _tipo(update_: dict[str, Any]) -> str:
    return next((t for t in _TIPOS if t in update_), "otro")


def _evento(bot_id: str, public_id: str, update_: dict[str, Any]) -> FlattenedEvent:
    """Solo diagnóstico: el tipo de update y de chat y si trae medios. Nunca el texto ni el usuario."""
    tipo = _tipo(update_)
    cuerpo = update_.get(tipo) if isinstance(update_.get(tipo), dict) else {}
    mensaje = cuerpo.get("message") if tipo == "callback_query" and isinstance(cuerpo.get("message"), dict) else cuerpo
    chat = mensaje.get("chat") if isinstance(mensaje, dict) and isinstance(mensaje.get("chat"), dict) else {}
    valor: dict[str, Any] = {"tipo": tipo, "chat_type": str(chat.get("type") or "")}
    if isinstance(mensaje, dict):
        valor["medios"] = [m for m in ("photo", "voice", "audio", "document", "sticker", "contact") if m in mensaje]
    return FlattenedEvent(
        object_type="telegram",
        field=tipo,
        external_id=f"tg:{bot_id}:{update_.get('update_id')}"[:200],
        entry_id=public_id,
        sub_id=None,
        value=valor,
    )


async def _contestar_toque(token: str, update_: dict[str, Any], tenant_id: uuid.UUID) -> None:
    """Quita el reloj del botón y el teclado del mensaje (para que no se toque dos veces). Best-effort con 2 s cada
    uno: si falla, el cliente ve el reloj o el teclado un rato, nada más."""
    toque = callback_query_id(update_)
    if not toque:
        return
    try:
        cliente = TelegramClient(token)
        await asyncio.wait_for(cliente.answer_callback_query(toque), _RESPUESTA_CALLBACK_S)
    except (ProviderError, TimeoutError):
        log.info("telegram.callback.answer_fallo", tenant_id=str(tenant_id))
        return
    mensaje = update_["callback_query"].get("message")
    chat = mensaje.get("chat") if isinstance(mensaje, dict) else None
    if not isinstance(chat, dict) or not isinstance(mensaje.get("message_id"), int):
        return
    try:
        await asyncio.wait_for(
            cliente.edit_message_reply_markup(chat.get("id"), mensaje["message_id"]), _RESPUESTA_CALLBACK_S
        )
    except (ProviderError, TimeoutError):
        log.info("telegram.callback.teclado_no_quitado", tenant_id=str(tenant_id))


async def _marcar_actividad(tenant_id: uuid.UUID, fila_id: uuid.UUID) -> None:
    """`last_update_at` para la ficha. Best-effort: no demora ni tumba la respuesta."""
    try:
        async with get_session() as session:
            await session.execute(
                update(TelegramConnection)
                .where(TelegramConnection.tenant_id == tenant_id, TelegramConnection.id == fila_id)
                .values(last_update_at=func.now())
            )
    except Exception:
        log.warning("telegram.webhook.actividad_no_marcada", tenant_id=str(tenant_id))


@router.post("/{public_id}", include_in_schema=False)
async def recibir(request: Request, public_id: str) -> Response:
    inicio = time.monotonic()
    resultado, respuesta = await _recibir(request, public_id)
    _medir(resultado, inicio)
    return respuesta


async def _recibir(request: Request, public_id: str) -> tuple[str, Response]:
    from core.billing.enforcement import is_tenant_operational
    from core.gateway.webhooks import (
        _alert_on_invalid_signature,
        _log_webhook_event,
        _mark_entry_processed,
        publish_inbound_message,
    )

    if not habilitado():
        log.info("telegram.webhook.rechazado", motivo="kill_switch")
        return "apagado", Response(status_code=503)
    if not secreto_webhook_valido(request.headers.get(HEADER_SECRETO)):
        # Telegram siempre lo manda con la forma de nuestros secretos: sin él no se consulta la base.
        log.info("telegram.webhook.rechazado", motivo="sin_secreto")
        return "secreto_invalido", Response(status_code=401)
    ruta = await _ruta(public_id)
    if ruta is None:
        log.info("telegram.webhook.rechazado", motivo="public_id_desconocido")
        return "desconocido", Response(status_code=404)
    tenant_id, connection_id = ruta

    async with tenant_scope(tenant_id):
        par = await _conexion(tenant_id, connection_id)
        if par is None:
            return "desconocido", Response(status_code=404)
        connection, fila = par
        bot_id = connection.external_id or ""
        if not bot_id:
            # Sin `bot_id` no hay llaves globales: falla cerrado (no debería pasar, el alta lo exige).
            log.error("telegram.webhook.conexion_sin_bot_id", tenant_id=str(tenant_id), connection_id=str(connection_id))
            return "error", Response(status_code=500)
        creds: dict[str, Any] = (
            get_secret_box().decrypt_json(connection.credentials_encrypted)
            if connection.credentials_encrypted
            else {}
        )
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not _ADAPTADOR.verify_request(b"", headers, str(creds.get("webhook_secret") or "")):
            log.warning(
                "telegram.webhook.rechazado",
                motivo="secreto_invalido",
                tenant_id=str(tenant_id),
                connection_id=str(connection_id),
                bot_id=bot_id,
            )
            await _alert_on_invalid_signature(f"telegram:{bot_id}")
            return "secreto_invalido", Response(status_code=401)

        raw_body = await _cuerpo_con_tope(request)
        if raw_body is None:
            log.info("telegram.webhook.rechazado", motivo="cuerpo_grande", tenant_id=str(tenant_id))
            return "grande", Response(status_code=413)
        try:
            update_ = json.loads(raw_body)
        except ValueError:
            update_ = None
        if not isinstance(update_, dict) or not isinstance(update_.get("update_id"), int):
            log.info("telegram.webhook.ignorado", motivo="json_invalido", tenant_id=str(tenant_id))
            return "ignorado", Response(status_code=200)

        if not connection.active:
            log.info("telegram.webhook.ignorado", motivo="pausada", tenant_id=str(tenant_id))
            return "pausado", Response(status_code=200)
        if not await is_tenant_operational(tenant_id):
            log.info("telegram.webhook.ignorado", motivo="negocio_no_operativo", tenant_id=str(tenant_id))
            return "pausado", Response(status_code=200)

        app_id = f"telegram:{bot_id}"
        evento = _evento(bot_id, public_id, update_)
        if not await _log_webhook_event(app_id, evento, tenant_id=tenant_id, process_error=None):
            log.error("telegram.webhook.auditoria_fallida", tenant_id=str(tenant_id))
            return "error", Response(status_code=500)
        await _marcar_actividad(tenant_id, fila.id)

        mensajes = _ADAPTADOR.parse_update(update_, bot_id=bot_id)
        if not mensajes:
            log.info("telegram.webhook.ignorado", motivo=evento.field, tenant_id=str(tenant_id))
            await _mark_entry_processed(app_id, tenant_id, [evento])
            return "ignorado", Response(status_code=200)

        for mensaje in mensajes:
            if mensaje.interactive_id:
                # Un id largo viajó como token en `callback_data` (PR core 4): se devuelve el original. Vencido: sin id.
                from core.gateway.telegram import interactivos

                mensaje.interactive_id = await interactivos.resolver(tenant_id, bot_id, mensaje.interactive_id)
        resultado = "aceptado"
        try:
            for mensaje in mensajes:
                publicado = await publish_inbound_message(tenant_id, mensaje, connection_id=connection_id)
                resultado = {"duplicate": "duplicado", "dropped": "descartado"}.get(publicado, "aceptado")
        except Exception:
            # Redis caído u otro fallo al publicar: el claim ya se liberó. 503 para que Telegram reintente.
            log.exception("telegram.webhook.publicacion_fallida", tenant_id=str(tenant_id))
            return "error", Response(status_code=503)
        await _contestar_toque(str(creds.get("bot_token") or ""), update_, tenant_id)
        await _mark_entry_processed(app_id, tenant_id, [evento])
        log.info(
            "telegram.webhook.recibido",
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
            bot_id=bot_id,
            tipo=evento.field,
            resultado=resultado,
        )
    return resultado, Response(status_code=200)
