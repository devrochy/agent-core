"""Operación de una conexión de Telegram (ADR-0199 §12): salud, reparar el webhook, mensaje de prueba, cambio de
token y desconexión.

Salud (`getMe` + `getWebhookInfo`). El estado queda en `telegram_connections.status`:

- `connected`: el token vale y el webhook apunta a nuestra URL, sin errores recientes.
- `degraded`: alguien cambió el webhook del bot, Telegram falló al entregar en los últimos 30 min, o hay más de 50
  updates esperando.
- `revoked`: Telegram rechazó el token (se revocó en @BotFather).
- `error`: el token es de otro bot u otro error de Telegram. Una caída transitoria NO cambia el estado.

Al pasar a `degraded` o `revoked` se alerta una vez al día por conexión. El cron `core.telegram_health_check` (cada
15 min) revisa todas las conexiones vivas y activas de todos los negocios, salvo con el kill-switch apagado.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from agentcore_channel_telegram.client import TelegramClient
from sqlalchemy import select, text

from core.db.models import ChannelConnection, TelegramConnection, utcnow
from core.db.session import get_session
from core.gateway.channels.base import ProviderError
from core.gateway.telegram import alta
from core.gateway.telegram.habilitado import habilitado
from core.shared.crypto import get_secret_box
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

_TEXTO_DE_PRUEBA = "Mensaje de prueba de solvershub: tu bot de Telegram funciona."
# Mismos topes que las pruebas de Meta y Twilio: una cada 30 s y 10 al día por conexión.
_PRUEBA_CADA_S = 30
_PRUEBAS_POR_DIA = 10
# Un error de entrega del webhook más reciente que esto degrada la conexión.
_ERROR_RECIENTE = timedelta(minutes=30)
# Más updates esperando que esto: Telegram no logra entregarnos (los descarta a las 24 h).
_PENDIENTES_MAX = 50
_NO_RESPONDE = ("TRANSIENT", "AMBIGUOUS", "RATE_LIMIT", "QUOTA")


@dataclass(frozen=True)
class _Conexion:
    connection: ChannelConnection
    fila: TelegramConnection
    # Fuera del repr: llevan el token y el secreto del webhook (un traceback con locales los imprimiría).
    creds: dict[str, Any] = field(repr=False)

    @property
    def token(self) -> str:
        return str(self.creds.get("bot_token") or "")

    @property
    def secreto(self) -> str:
        return str(self.creds.get("webhook_secret") or "")


async def _cargar(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> _Conexion | None:
    alta.exigir_scope(tenant_id)
    async with get_session() as session:
        par = (
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
    if par is None:
        return None
    connection, fila = par
    creds = (
        get_secret_box().decrypt_json(connection.credentials_encrypted)
        if connection.credentials_encrypted
        else {}
    )
    return _Conexion(connection, fila, creds)


def _cliente_de(c: _Conexion, transport: httpx.AsyncBaseTransport | None) -> TelegramClient:
    return alta.cliente(c.token, transport)


def _desde_unix(valor: object) -> datetime | None:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        return None
    return datetime.fromtimestamp(valor, UTC)


@dataclass(frozen=True)
class _Revision:
    status: str | None  # None = Telegram no respondió: no se cambia el estado
    motivo: str = ""
    pending_update_count: int | None = None
    last_error_message: str | None = None
    last_error_at: datetime | None = None
    bot_username: str = ""


async def _guardar(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, r: _Revision, *, desde: Any = None
) -> str | None:
    """Persiste una revisión sobre la fila releída con candado. Devuelve el estado anterior, o `None` si la fila ya
    no existe o cambió desde `desde` (un cambio de token en medio): esa revisión ya no vale."""
    async with get_session() as session:
        fila = (
            await session.execute(
                select(TelegramConnection)
                .where(
                    TelegramConnection.tenant_id == tenant_id,
                    TelegramConnection.channel_connection_id == connection_id,
                    TelegramConnection.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if fila is None:
            return None
        if desde is not None and fila.updated_at != desde:
            log.info(
                "telegram.salud.descartada_por_cambio",
                tenant_id=str(tenant_id),
                connection_id=str(connection_id),
            )
            return None
        anterior = fila.status
        fila.last_checked_at = utcnow()
        if r.status is not None:
            fila.status = r.status
        if r.pending_update_count is not None:
            fila.pending_update_count = max(0, r.pending_update_count)
        if r.last_error_message is not None:
            fila.last_error_message = r.last_error_message[:300]
            fila.last_error_at = r.last_error_at
        if r.bot_username:
            fila.bot_username = r.bot_username
    return anterior


async def _alertar(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, anterior: str, nuevo: str, motivo: str
) -> None:
    if nuevo not in ("degraded", "revoked") or anterior == nuevo:
        return
    from core.shared.alerts import send_alert_once

    await send_alert_once(
        f"telegram_{nuevo}:{connection_id}",
        "Bot de Telegram con problemas",
        f"Negocio {tenant_id}, conexión {connection_id}: {nuevo} ({motivo}).",
        severity="critical" if nuevo == "revoked" else "warning",
    )


async def _diagnosticar(c: _Conexion, transport: httpx.AsyncBaseTransport | None) -> _Revision:
    try:
        client = _cliente_de(c, transport)
        me = await client.get_me()
        info = await client.get_webhook_info() or {}
    except alta.AltaError:
        return _Revision("revoked", "el token guardado no tiene la forma de un token de bot")
    except ProviderError as exc:
        if exc.category == "AUTH":
            return _Revision("revoked", "Telegram rechazó el token (¿se revocó en @BotFather?)")
        if exc.category in _NO_RESPONDE:
            return _Revision(None)
        return _Revision("error", f"Telegram respondió {exc.http_status}")
    if str((me or {}).get("id")) != (c.connection.external_id or ""):
        return _Revision("error", "el token es de otro bot")
    pendientes = info.get("pending_update_count")
    pendientes = pendientes if isinstance(pendientes, int) and not isinstance(pendientes, bool) else 0
    error_at = _desde_unix(info.get("last_error_date"))
    error_msg = str(info.get("last_error_message") or "") or None
    base = {
        "pending_update_count": pendientes,
        "last_error_message": error_msg,
        "last_error_at": error_at if error_msg else None,
        "bot_username": str((me or {}).get("username") or ""),
    }
    if str(info.get("url") or "") != c.fila.webhook_url:
        return _Revision("degraded", "el webhook del bot ya no apunta a solvershub", **base)
    if error_at is not None and utcnow() - error_at < _ERROR_RECIENTE:
        return _Revision("degraded", "Telegram no pudo entregar mensajes hace poco", **base)
    if pendientes > _PENDIENTES_MAX:
        return _Revision("degraded", f"{pendientes} mensajes esperando entrega", **base)
    return _Revision("connected", **base)


async def revisar(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> alta.ConexionTelegram | None:
    """Revisa el token y el webhook contra Telegram y guarda el estado. `None` si la conexión no existe."""
    c = await _cargar(tenant_id, connection_id)
    if c is None:
        return None
    r = await _diagnosticar(c, transport)
    anterior = await _guardar(tenant_id, connection_id, r, desde=c.fila.updated_at)
    if anterior is not None and r.status is not None and anterior != r.status:
        evento = {
            "revoked": "telegram.salud.revocada",
            "degraded": "telegram.salud.degradada",
        }.get(r.status, "telegram.salud.cambio")
        log.info(
            evento,
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
            bot_id=c.connection.external_id,
            antes=anterior,
            ahora=r.status,
        )
        await _alertar(tenant_id, connection_id, anterior, r.status, r.motivo)
    return await alta.obtener(tenant_id, connection_id)


async def revisar_todas() -> int:
    """Cron: revisa todas las conexiones vivas y activas. Una que falla no corta las demás. Devuelve cuántas. Con el
    kill-switch apagado no consulta a Telegram."""
    if not habilitado():
        log.info("telegram.salud.kill_switch_apagado")
        return 0
    async with get_session() as session:
        filas = (
            await session.execute(text("select id, tenant_id from admin_telegram_connection_ids()"))
        ).all()
    revisadas = 0
    for connection_id, tenant_id in filas:
        try:
            async with tenant_scope(tenant_id):
                await revisar(tenant_id, connection_id)
            revisadas += 1
        except Exception:
            log.exception(
                "telegram.salud.fallo", tenant_id=str(tenant_id), connection_id=str(connection_id)
            )
    log.info("telegram.salud.ronda", revisadas=revisadas, total=len(filas))
    return revisadas


async def reparar_webhook(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> alta.ConexionTelegram | None:
    """Vuelve a registrar el webhook del bot (con el secreto guardado) y lo confirma."""
    c = await _cargar(tenant_id, connection_id)
    if c is None:
        return None
    if not c.connection.active:
        raise alta.AltaError(409, "conexion_pausada", "La conexión está pausada.")
    alta.exigir_url_publica()
    estado = await alta.registrar_webhook(
        _cliente_de(c, transport),
        tenant_id,
        connection_id,
        c.fila.webhook_url,
        c.secreto,
        bot_id=c.connection.external_id or "",
    )
    if not estado:
        return None
    await _alertar(tenant_id, connection_id, c.fila.status, estado, "reparar el webhook")
    return await alta.obtener(tenant_id, connection_id)


async def enviar_prueba(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    chat_id: int,
    *,
    actor: str = "admin",
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Manda un texto de prueba a un chat. Telegram solo deja escribirle a quien ya le escribió al bot (`/start`).
    Devuelve el `message_id`. La respuesta de Telegram es síncrona: no hay seguimiento posterior."""
    from core.memory.working import get_redis

    c = await _cargar(tenant_id, connection_id)
    if c is None:
        raise alta.AltaError(404, "not_found", "Conexión no encontrada.")
    if not c.connection.active:
        raise alta.AltaError(409, "conexion_pausada", "La conexión está pausada.")
    redis = get_redis()
    if not await redis.set(
        f"telegram_prueba:{tenant_id}:{connection_id}", "1", nx=True, ex=_PRUEBA_CADA_S
    ):
        raise alta.AltaError(429, "too_many_tests", "Espera unos segundos antes de mandar otra prueba.")
    clave_dia = f"telegram_prueba_dia:{tenant_id}:{connection_id}"
    await redis.set(clave_dia, 0, nx=True, ex=86400)
    if await redis.incr(clave_dia) > _PRUEBAS_POR_DIA:
        raise alta.AltaError(429, "daily_limit", "Ya mandaste las pruebas de hoy.")
    try:
        enviado = await _cliente_de(c, transport).send_message(chat_id, _TEXTO_DE_PRUEBA)
    except ProviderError as exc:
        if exc.category == "RECIPIENT":
            raise alta.AltaError(
                409,
                "chat_no_iniciado",
                "Ese chat no le ha escrito al bot. Ábrelo en Telegram, toca Iniciar (/start) y vuelve a probar.",
            ) from None
        if exc.category == "AUTH":
            raise alta.AltaError(
                409, "token_revocado", "Telegram rechazó el token del bot. Cámbialo en Credenciales."
            ) from None
        if exc.category in _NO_RESPONDE:
            raise alta.AltaError(
                502, "telegram_unavailable", "Telegram no respondió. Intenta de nuevo en unos minutos."
            ) from None
        raise alta.AltaError(
            502, "telegram_error", f"Telegram respondió con un error ({exc.http_status})."
        ) from None
    from core.shared.audit import record_audit

    await record_audit(
        actor,
        "telegram.test_message",
        input_data={"connection_id": str(connection_id), "chat_last4": str(chat_id)[-4:]},
    )
    message_id = (enviado or {}).get("message_id")
    return message_id if isinstance(message_id, int) else 0


async def rotar_credenciales(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    bot_token: str,
    confirm_overwrite_webhook: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> alta.ConexionTelegram:
    """Cambia el token del MISMO bot (p. ej. tras revocarlo en @BotFather): se valida con `getMe` antes de guardar,
    el `public_id` se conserva y el secreto del webhook se rota. Un token de otro bot es 409: eso es otra conexión."""
    c = await _cargar(tenant_id, connection_id)
    if c is None:
        raise alta.AltaError(404, "not_found", "Conexión no encontrada.")
    if not c.connection.active:
        # El alta reactiva la conexión: cambiar el token de una pausada la pondría a atender sin que se pidiera.
        raise alta.AltaError(
            409, "conexion_pausada", "Reanuda la conexión antes de cambiar su token."
        )
    me = await alta.leer_bot(alta.cliente(bot_token, transport))
    if str(me["id"]) != (c.connection.external_id or ""):
        raise alta.AltaError(
            409,
            "otro_bot",
            "Ese token es de otro bot. Para conectar otro bot, agrégalo como una conexión nueva.",
        )
    return await alta.conectar(
        tenant_id,
        bot_token=bot_token,
        label=c.connection.label,
        confirm_overwrite_webhook=confirm_overwrite_webhook,
        transport=transport,
    )


async def desconectar(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    continuar_si_falla: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Quita el webhook del bot (`deleteWebhook`) y borra lógicamente la conexión y su fila (libera el bot). Quitar el
    webhook va primero para poder reintentar: si falla y no se pidió `continuar_si_falla`, no se borra nada (502
    `webhook_no_eliminado`). Solo se quita si el bot sigue apuntando a nuestra URL. Devuelve si lo quitó."""
    from core.gateway.connections import delete_connection

    c = await _cargar(tenant_id, connection_id)
    if c is None:
        raise alta.AltaError(404, "not_found", "Conexión no encontrada.")
    quitado = False
    try:
        client = _cliente_de(c, transport)
        info = await client.get_webhook_info() or {}
        if str(info.get("url") or "") == c.fila.webhook_url:
            quitado = await client.delete_webhook()
    except (ProviderError, alta.AltaError) as exc:
        revocado = isinstance(exc, ProviderError) and exc.category == "AUTH"
        log.warning(
            "telegram.desconectar.webhook_no_eliminado",
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
            revocado=revocado,
        )
        # Con el token revocado no hay webhook que quitar con él: Telegram ya no entrega nada a este bot con ese token.
        if not revocado and not continuar_si_falla:
            raise alta.AltaError(
                502,
                "webhook_no_eliminado",
                "No se pudo quitar el webhook del bot en Telegram. Intenta de nuevo, o desconecta igual.",
            ) from None
    await delete_connection(connection_id)
    log.info(
        "telegram.conexion.desconectada",
        tenant_id=str(tenant_id),
        connection_id=str(connection_id),
        webhook_quitado=quitado,
    )
    return quitado
