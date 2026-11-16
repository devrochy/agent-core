"""Alta de un bot de Telegram del negocio (ADR-0199 §1-2).

El negocio pega el token que le dio @BotFather; el backend hace el resto, en este orden:

1. Valida el token con `getMe` (sin escribir nada). El `bot_id` sale de su respuesta, nunca del token: la
   documentación de la Bot API no garantiza que el número antes de `:` sea el id del bot.
2. Lee `getWebhookInfo`: si el bot ya manda sus updates a otra dirección (otra integración del negocio), no sigue sin
   `confirm_overwrite_webhook` (409 `webhook_ajeno`). Una URL nuestra no es ajena.
3. **Reclama la conexión antes de tocar el webhook.** En una transacción, con el candado `telegram_bot:<bot_id>`,
   guarda la conexión (credenciales cifradas: token, `bot_id` y un secreto de webhook nuevo) y su fila
   `telegram_connections`. Un bot vivo en otro negocio choca con `uq_channel_connections_telegram_bot_viva` y es 409
   `bot_en_otro_negocio` SIN haber pisado su webhook.
4. Fuera de la transacción, registra el webhook con el secreto y lo confirma con `getWebhookInfo`. Si falla, la
   conexión queda en `error` (o `revoked` si el token dejó de valer) y el alta responde 502: repetirla es idempotente.

Repetir el alta con el mismo bot en el mismo negocio devuelve la misma conexión y el mismo `public_id`, con un
secreto nuevo (Telegram reintenta con él lo que estuviera en vuelo).
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from agentcore_channel_telegram.client import TelegramClient, redactar
from agentcore_channel_telegram.limites import nuevo_secreto_webhook
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from core.config import get_settings
from core.db.models import ChannelConnection, TelegramConnection, utcnow
from core.db.session import get_session
from core.gateway.channels.base import ProviderError
from core.gateway.connection_types import TELEGRAM
from core.gateway.connections import save_connection_in
from core.gateway.telegram.habilitado import habilitado
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

_INDICE_BOT = "uq_channel_connections_telegram_bot_viva"
_RUTA_WEBHOOK = "/webhooks/telegram/"
_NO_RESPONDE = ("TRANSIENT", "AMBIGUOUS", "RATE_LIMIT", "QUOTA")


class AltaError(Exception):
    """Error del alta con su respuesta HTTP: `http_status`, `code` estable para el panel y mensaje en español."""

    def __init__(
        self, http_status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class ConexionTelegram:
    connection_id: uuid.UUID
    label: str
    bot_id: str
    bot_username: str
    bot_name: str
    status: str
    webhook_url: str
    webhook_confirmado_at: datetime | None
    pending_update_count: int
    last_error_message: str | None
    last_error_at: datetime | None
    last_update_at: datetime | None
    last_checked_at: datetime | None
    active: bool
    created: bool = False


# --------------------------------------------------------------------------- utilidades
def no_disponible() -> AltaError:
    return AltaError(404, "not_found", "Telegram no está disponible en este momento.")


def exigir_habilitado() -> None:
    if not habilitado():
        raise no_disponible()


def exigir_scope(tenant_id: uuid.UUID) -> None:
    """El alta escribe con el negocio del `tenant_scope` (RLS y `save_connection_in`): el `tenant_id` del argumento
    tiene que ser ese mismo."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("alta de Telegram: fuera del tenant_scope del negocio")


def base_webhooks() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}{_RUTA_WEBHOOK}"


def url_webhook(public_id: str) -> str:
    return f"{base_webhooks()}{public_id}"


def es_nuestra(url: str) -> bool:
    return bool(url) and url.startswith(base_webhooks())


def exigir_url_publica() -> None:
    """Telegram solo entrega webhooks por https: sin una URL pública https el alta no puede terminar."""
    if not base_webhooks().startswith("https://"):
        raise AltaError(
            503,
            "url_publica_no_https",
            "Este ambiente no tiene una dirección pública https para recibir los mensajes de Telegram.",
        )


def cliente(token: str, transport: httpx.AsyncBaseTransport | None = None) -> TelegramClient:
    try:
        return TelegramClient(token.strip(), transport=transport)
    except ProviderError:
        raise _token_invalido() from None


def _token_invalido() -> AltaError:
    return AltaError(
        422,
        "telegram_token_invalido",
        "Telegram no reconoce ese token. Cópialo completo desde @BotFather (se ve como 123456789:AAH…).",
    )


def _no_responde() -> AltaError:
    return AltaError(
        502, "telegram_unavailable", "Telegram no respondió. Intenta de nuevo en unos minutos."
    )


async def leer_bot(client: TelegramClient) -> dict[str, Any]:
    """`getMe`: el token tiene que ser de un bot. Un 401 o un 404 (token con otra forma) es un token inválido."""
    try:
        me = await client.get_me()
    except ProviderError as exc:
        if exc.category in _NO_RESPONDE:
            raise _no_responde() from None
        if exc.category in ("AUTH", "CONFIG"):
            raise _token_invalido() from None
        raise AltaError(
            502, "telegram_error", f"Telegram respondió con un error ({exc.http_status})."
        ) from None
    if not isinstance(me, dict) or me.get("is_bot") is not True or not isinstance(me.get("id"), int):
        raise _token_invalido()
    return me


async def _webhook_actual(client: TelegramClient) -> str:
    try:
        info = await client.get_webhook_info()
    except ProviderError as exc:
        if exc.category in _NO_RESPONDE:
            raise _no_responde() from None
        raise AltaError(
            502, "telegram_error", f"Telegram respondió con un error ({exc.http_status})."
        ) from None
    return str((info or {}).get("url") or "")


def _nombre(me: dict[str, Any]) -> str:
    partes = [str(me.get("first_name") or ""), str(me.get("last_name") or "")]
    return " ".join(p for p in partes if p).strip()


# --------------------------------------------------------------------------- alta
async def conectar(
    tenant_id: uuid.UUID,
    *,
    bot_token: str,
    label: str = "",
    confirm_overwrite_webhook: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConexionTelegram:
    exigir_habilitado()
    exigir_scope(tenant_id)
    exigir_url_publica()
    client = cliente(bot_token, transport)
    me = await leer_bot(client)
    bot_id = str(me["id"])
    username = str(me.get("username") or "")

    actual = await _webhook_actual(client)
    if actual and not es_nuestra(actual) and not confirm_overwrite_webhook:
        raise AltaError(
            409,
            "webhook_ajeno",
            "Ese bot ya envía sus mensajes a otra dirección. Confirma que quieres reemplazarla.",
            {"webhook_url": actual},
        )

    secreto = nuevo_secreto_webhook()
    try:
        async with get_session() as session:
            await session.execute(
                text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"telegram_bot:{bot_id}"}
            )
            pausada = (
                await session.execute(
                    select(ChannelConnection.id).where(
                        ChannelConnection.tenant_id == tenant_id,
                        ChannelConnection.provider == "telegram",
                        ChannelConnection.external_id == bot_id,
                        ChannelConnection.deleted_at.is_(None),
                        ChannelConnection.active.is_(False),
                    )
                )
            ).first()
            if pausada is not None:
                # Repetir el alta la reactivaría: no se reactiva sin que se pida.
                raise AltaError(
                    409, "conexion_pausada", "Reanuda la conexión antes de volver a conectarla."
                )
            connection, created = await save_connection_in(
                session,
                label.strip() or (f"Telegram @{username}" if username else "Telegram"),
                {"bot_token": bot_token.strip(), "bot_id": bot_id, "webhook_secret": secreto},
                type_id=TELEGRAM,
            )
            await session.flush()
            fila = await _fila(session, tenant_id, connection.id)
            public_id = fila.public_id if fila is not None else secrets.token_urlsafe(18)
            if fila is None:
                fila = TelegramConnection(
                    tenant_id=tenant_id, channel_connection_id=connection.id, public_id=public_id
                )
                session.add(fila)
            fila.bot_username = username
            fila.bot_name = _nombre(me)
            fila.webhook_url = url_webhook(public_id)
            fila.status = "pending"
            fila.webhook_confirmado_at = None
            await session.flush()
            connection_id = connection.id
    except IntegrityError as exc:
        if _INDICE_BOT in str(exc.orig):
            log.info("telegram.alta.bot_en_otro_negocio", tenant_id=str(tenant_id), bot_id=bot_id)
            raise AltaError(
                409,
                "bot_en_otro_negocio",
                "Este bot ya está conectado en otro negocio. Desconéctalo allá o crea otro bot en @BotFather.",
            ) from None
        restriccion = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        log.warning("telegram.alta.integrity_error", constraint=restriccion)
        raise AltaError(409, "conflicto", "No se pudo guardar la conexión. Intenta de nuevo.") from None

    if actual and not es_nuestra(actual):
        log.info("telegram.alta.webhook_ajeno", tenant_id=str(tenant_id), bot_id=bot_id)
    estado = await registrar_webhook(
        client, tenant_id, connection_id, url_webhook(public_id), secreto, bot_id=bot_id
    )
    resultado = await obtener(tenant_id, connection_id)
    assert resultado is not None
    resultado = _con_created(resultado, created)
    log.info(
        "telegram.alta.confirmada" if estado == "connected" else "telegram.alta.sin_confirmar",
        tenant_id=str(tenant_id),
        connection_id=str(connection_id),
        bot_id=bot_id,
        created=created,
        status=estado,
    )
    if estado != "connected":
        raise AltaError(
            502,
            "telegram_webhook_no_registrado",
            "El bot quedó guardado, pero Telegram no confirmó la dirección de los mensajes. Intenta de nuevo.",
            {"connection_id": str(connection_id), "status": estado},
        )
    return resultado


def _con_created(c: ConexionTelegram, created: bool) -> ConexionTelegram:
    from dataclasses import replace

    return replace(c, created=created)


async def registrar_webhook(
    client: TelegramClient,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    url: str,
    secreto: str,
    *,
    bot_id: str,
) -> str:
    """`setWebhook` con el secreto y `getWebhookInfo` para confirmarlo. Guarda y devuelve el estado: `connected`
    solo si Telegram quedó apuntando a `url`; `revoked` si el token dejó de valer; si no, `error`.

    Todo bajo el candado `telegram_bot:<bot_id>` y solo si la fila sigue viva: un registro que llega tarde (el negocio
    desconectó el bot y otro lo reclamó) no pisa el webhook del otro negocio. Devuelve `""` si la fila ya no existe."""
    async with get_session() as session:
        await session.execute(
            text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": f"telegram_bot:{bot_id}"}
        )
        fila = await _fila(session, tenant_id, connection_id, para_actualizar=True)
        if fila is None:
            log.info("telegram.webhook.fila_borrada", connection_id=str(connection_id))
            return ""
        mensaje: str | None = None
        try:
            await client.set_webhook(url, secreto)
            info = await client.get_webhook_info()
            estado = "connected" if str((info or {}).get("url") or "") == url else "error"
            if estado == "error":
                mensaje = "Telegram no devolvió la dirección registrada."
        except ProviderError as exc:
            estado = "revoked" if exc.category == "AUTH" else "error"
            mensaje = redactar(str(exc))[:300]
            log.warning(
                "telegram.webhook.no_registrado",
                connection_id=str(connection_id),
                category=exc.category,
                status=exc.http_status,
            )
        ahora = utcnow()
        fila.status = estado
        fila.last_checked_at = ahora
        if estado == "connected":
            fila.webhook_confirmado_at = ahora
            fila.last_error_message = None
            fila.last_error_at = None
        else:
            fila.last_error_message = mensaje
            fila.last_error_at = ahora
    return estado


# --------------------------------------------------------------------------- lectura
async def _fila(
    session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID, *, para_actualizar: bool = False
) -> TelegramConnection | None:
    consulta = select(TelegramConnection).where(
        TelegramConnection.tenant_id == tenant_id,
        TelegramConnection.channel_connection_id == connection_id,
        TelegramConnection.deleted_at.is_(None),
    )
    if para_actualizar:
        consulta = consulta.with_for_update()
    return (await session.execute(consulta)).scalar_one_or_none()


def a_conexion(fila: TelegramConnection, conexion: ChannelConnection) -> ConexionTelegram:
    return ConexionTelegram(
        connection_id=fila.channel_connection_id,
        label=conexion.label,
        bot_id=conexion.external_id or "",
        bot_username=fila.bot_username,
        bot_name=fila.bot_name,
        status=fila.status,
        webhook_url=fila.webhook_url,
        webhook_confirmado_at=fila.webhook_confirmado_at,
        pending_update_count=fila.pending_update_count,
        last_error_message=fila.last_error_message,
        last_error_at=fila.last_error_at,
        last_update_at=fila.last_update_at,
        last_checked_at=fila.last_checked_at,
        active=bool(conexion.active),
    )


def _consulta(tenant_id: uuid.UUID) -> Any:
    return (
        select(TelegramConnection, ChannelConnection)
        .join(
            ChannelConnection,
            (ChannelConnection.id == TelegramConnection.channel_connection_id)
            & (ChannelConnection.tenant_id == TelegramConnection.tenant_id),
        )
        .where(
            TelegramConnection.tenant_id == tenant_id,
            TelegramConnection.deleted_at.is_(None),
            ChannelConnection.deleted_at.is_(None),
            ChannelConnection.provider == "telegram",
        )
    )


async def obtener(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> ConexionTelegram | None:
    async with get_session() as session:
        row = (
            await session.execute(
                _consulta(tenant_id).where(TelegramConnection.channel_connection_id == connection_id)
            )
        ).first()
    if row is None:
        return None
    fila, conexion = row
    return a_conexion(fila, conexion)


async def listar(tenant_id: uuid.UUID) -> list[ConexionTelegram]:
    async with get_session() as session:
        rows = (
            await session.execute(_consulta(tenant_id).order_by(TelegramConnection.created_at))
        ).all()
    return [a_conexion(fila, conexion) for fila, conexion in rows]
