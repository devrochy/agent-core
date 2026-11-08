"""Alta guiada de WhatsApp vía Twilio con la cuenta del negocio (ADR-0186 §3).

Dos pasos y sin estado intermedio:

1. `descubrir`: valida el Account SID y el Auth Token contra Twilio y lista los senders de WhatsApp de la cuenta
   (Senders API v2). Solo se pueden elegir los `ONLINE`. No escribe nada.
2. `conectar`: con el sender elegido, en UNA transacción guarda la conexión (`save_connection_in`, credenciales
   cifradas) y su fila `twilio_connections`; después registra los webhooks del sender y los relee para
   confirmarlos. Si registrar falla, la conexión queda en `error` con el código de Twilio y se reintenta con el
   mismo alta o, en el PR-5, con «re-registrar webhooks».

Repetir el alta con el mismo número en el mismo negocio devuelve la conexión existente (con las credenciales y el
estado actualizados). El mismo número en otro negocio es 409: lo impide el índice único global de la migración
0121, y el candado `twilio_asset:sender:<e164>` serializa dos altas simultáneas. La misma cuenta de Twilio en otro
negocio también es 409 (`cuenta_en_otro_negocio`): su Auth Token firma los webhooks de todos sus números.

Si el sender ya tenía webhooks que no son nuestros, el alta no sigue sin `confirm_overwrite_webhook`: puede ser otra
integración del negocio. Los anteriores se guardan en `previous_webhook` para restaurarlos al desconectar.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from agentcore_channel_twilio.client import TwilioClient
from agentcore_channel_twilio.senders import Sender, get_sender, list_whatsapp_senders, set_webhooks
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from core.config import get_settings
from core.db.models import ChannelConnection, TwilioConnection, utcnow
from core.db.session import get_session
from core.gateway.channels.base import ProviderError
from core.gateway.connection_types import WHATSAPP_TWILIO
from core.gateway.connections import save_connection_in
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

_INDICE_NUMERO = "uq_channel_connections_twilio_sender_viva"
# Campos del webhook del sender que el alta reemplaza.
_CAMPOS_WEBHOOK = ("callback_url", "status_callback_url", "fallback_url")


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
class Cuenta:
    friendly_name: str
    status: str
    type: str


@dataclass(frozen=True)
class SenderDisponible:
    sender_sid: str
    e164: str
    status: str
    display_name: str
    quality_rating: str
    seleccionable: bool
    # "" | "no_online" | "ya_conectado_aqui"
    motivo: str = ""
    webhook_ajeno: bool = False


@dataclass(frozen=True)
class ConexionTwilio:
    connection_id: uuid.UUID
    label: str
    sender_e164: str
    sender_sid: str
    sender_status: str
    display_name: str
    status: str
    account_sid: str
    webhook_inbound_url: str
    webhook_status_url: str
    tenia_webhook_previo: bool
    last_error_code: int | None
    active: bool
    created: bool = False
    detalles: dict[str, Any] = field(default_factory=dict)


def _base_webhooks() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/webhooks/twilio/"


def urls_webhook(public_id: str) -> tuple[str, str]:
    """URL de entrada y de estados de una conexión. Son las que se registran en el sender y contra las que se
    verifica la firma (ADR-0186 §5)."""
    base = _base_webhooks()
    return f"{base}{public_id}/inbound", f"{base}{public_id}/status"


def _webhook_ajeno(sender: Sender) -> dict[str, str]:
    """Webhooks del sender que no apuntan a solvershub: se guardan y se pide confirmación antes de pisarlos."""
    base = _base_webhooks()
    return {
        k: str(sender.webhook.get(k) or "")
        for k in _CAMPOS_WEBHOOK
        if sender.webhook.get(k) and not str(sender.webhook.get(k)).startswith(base)
    }


def _traducir(exc: ProviderError) -> AltaError:
    """Error de Twilio → respuesta del alta. Credenciales malas es 422, no 401: un 401 de nuestra API significa
    «tu sesión del panel expiró»."""
    if exc.category == "AUTH":
        return AltaError(
            422, "twilio_credentials_invalid", "Twilio rechazó el Account SID o el Auth Token."
        )
    if exc.category == "CONFIG":
        return AltaError(
            422,
            "twilio_credentials_invalid",
            "El Account SID o el Auth Token no tienen el formato de Twilio.",
        )
    if exc.category in ("TRANSIENT", "AMBIGUOUS", "RATE_LIMIT", "QUOTA"):
        return AltaError(
            502, "twilio_unavailable", "Twilio no respondió. Intenta de nuevo en unos minutos."
        )
    if exc.http_status == 404 or exc.code == 20404:
        return AltaError(422, "sender_not_found", "Ese número no está en la cuenta de Twilio.")
    return AltaError(
        502, "twilio_error", f"Twilio respondió con un error ({exc.code or exc.http_status})."
    )


async def _cuenta(client: TwilioClient) -> Cuenta:
    body = await client.get(f"{client.account_url}.json", op="accounts.get")
    cuenta = Cuenta(
        friendly_name=str(body.get("friendly_name") or ""),
        status=str(body.get("status") or "").lower(),
        type=str(body.get("type") or ""),
    )
    if cuenta.status != "active":
        raise AltaError(409, "twilio_account_suspended", "La cuenta de Twilio no está activa.")
    return cuenta


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    """El alta escribe con el negocio del `tenant_scope` (RLS y `save_connection_in`): el `tenant_id` del
    argumento tiene que ser ese mismo."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("alta de Twilio: fuera del tenant_scope del negocio")


def _cliente(
    account_sid: str, auth_token: str, transport: httpx.AsyncBaseTransport | None
) -> TwilioClient:
    try:
        return TwilioClient(account_sid.strip(), auth_token.strip(), transport=transport)
    except ProviderError as exc:
        raise _traducir(exc) from exc


async def _numeros_del_negocio(tenant_id: uuid.UUID) -> set[str]:
    _exigir_scope(tenant_id)
    async with get_session() as session:
        rows = await session.execute(
            select(ChannelConnection.external_id).where(
                ChannelConnection.tenant_id == tenant_id,
                ChannelConnection.provider == "twilio",
                ChannelConnection.deleted_at.is_(None),
            )
        )
        return {str(r[0]) for r in rows if r[0]}


async def descubrir(
    tenant_id: uuid.UUID,
    account_sid: str,
    auth_token: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[Cuenta, list[SenderDisponible]]:
    _exigir_scope(tenant_id)
    client = _cliente(account_sid, auth_token, transport)
    try:
        cuenta = await _cuenta(client)
        senders = await list_whatsapp_senders(client)
    except ProviderError as exc:
        raise _traducir(exc) from exc
    propios = await _numeros_del_negocio(tenant_id)
    disponibles = []
    for s in senders:
        motivo = "ya_conectado_aqui" if s.e164 in propios else ("" if s.online else "no_online")
        disponibles.append(
            SenderDisponible(
                sender_sid=s.sid,
                e164=s.e164,
                status=s.status,
                display_name=s.display_name,
                quality_rating=s.quality_rating,
                # Uno ya conectado aquí se puede volver a elegir: el alta es idempotente y refresca la conexión.
                seleccionable=s.online,
                motivo=motivo,
                webhook_ajeno=bool(_webhook_ajeno(s)),
            )
        )
    log.info("twilio_discover", tenant_id=str(tenant_id), senders=len(disponibles))
    return cuenta, disponibles


async def conectar(
    tenant_id: uuid.UUID,
    *,
    account_sid: str,
    auth_token: str,
    sender_sid: str,
    label: str = "",
    confirm_overwrite_webhook: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ConexionTwilio:
    _exigir_scope(tenant_id)
    client = _cliente(account_sid, auth_token, transport)
    try:
        await _cuenta(client)
        sender = await get_sender(client, sender_sid)
    except ProviderError as exc:
        raise _traducir(exc) from exc
    if not sender.online:
        raise AltaError(
            422,
            "sender_not_online",
            "Ese número todavía no está activo en Twilio.",
            {"status": sender.status},
        )
    ajeno = _webhook_ajeno(sender)
    if ajeno and not confirm_overwrite_webhook:
        raise AltaError(
            422,
            "webhook_overwrite_not_confirmed",
            "El número ya envía sus mensajes a otra dirección. Confirma que quieres reemplazarla.",
            {"previous_webhook": ajeno},
        )

    e164 = sender.e164
    try:
        async with get_session() as session:
            for clave in (
                f"twilio_asset:account:{client.account_sid}",
                f"twilio_asset:sender:{e164}",
            ):
                await session.execute(
                    text("select pg_advisory_xact_lock(hashtext(:k))"), {"k": clave}
                )
            # Una cuenta, un negocio: su Auth Token firma los webhooks de todos sus números (migración 0121).
            en_otro = await session.execute(
                text("select twilio_account_en_otro_negocio(:a)"),
                {"a": client.account_sid},
            )
            if en_otro.scalar():
                raise AltaError(
                    409,
                    "cuenta_en_otro_negocio",
                    "Esta cuenta de Twilio ya está conectada en otro negocio. Usa una cuenta propia para este.",
                )
            connection, created = await save_connection_in(
                session,
                label.strip() or f"WhatsApp {e164}",
                {
                    "account_sid": client.account_sid,
                    "auth_token": auth_token.strip(),
                    "sender_e164": e164,
                },
                type_id=WHATSAPP_TWILIO,
            )
            await session.flush()
            fila = (
                await session.execute(
                    select(TwilioConnection).where(
                        TwilioConnection.tenant_id == tenant_id,
                        TwilioConnection.channel_connection_id == connection.id,
                        TwilioConnection.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            public_id = fila.public_id if fila is not None else secrets.token_urlsafe(18)
            inbound_url, status_url = urls_webhook(public_id)
            if fila is None:
                fila = TwilioConnection(
                    tenant_id=tenant_id,
                    channel_connection_id=connection.id,
                    public_id=public_id,
                    account_sid=client.account_sid,
                    sender_sid=sender.sid,
                    sender_e164=e164,
                    webhook_inbound_url=inbound_url,
                    webhook_status_url=status_url,
                    previous_webhook=ajeno,
                )
                session.add(fila)
            else:
                fila.account_sid = client.account_sid
                fila.sender_sid = sender.sid
                fila.sender_e164 = e164
                fila.webhook_inbound_url = inbound_url
                fila.webhook_status_url = status_url
                if ajeno:
                    fila.previous_webhook = ajeno
            fila.status = "pending"
            fila.sender_status = sender.status
            fila.display_name = sender.display_name
            await session.flush()
            connection_id, label_final, active = connection.id, connection.label, connection.active
    except IntegrityError as exc:
        if _INDICE_NUMERO in str(exc.orig):
            raise AltaError(
                409, "sender_ya_conectado", "Este número ya está conectado en otro negocio."
            ) from None
        # Otro choque de la base (CHECK de formato, `public_id` repetido): sin el texto de la base en la respuesta.
        restriccion = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        log.warning("twilio_alta_integrity_error", constraint=restriccion)
        raise AltaError(
            409, "conflicto", "No se pudo guardar la conexión. Intenta de nuevo."
        ) from None

    status, last_error, sender_status, display_name = await _registrar_webhooks(
        client, sender.sid, inbound_url, status_url
    )
    async with get_session() as session:
        fila = (
            await session.execute(
                select(TwilioConnection).where(
                    TwilioConnection.tenant_id == tenant_id,
                    TwilioConnection.channel_connection_id == connection_id,
                    TwilioConnection.deleted_at.is_(None),
                )
            )
        ).scalar_one()
        fila.status = status
        fila.last_error_code = last_error
        fila.last_health_at = utcnow()
        if sender_status:
            fila.sender_status = sender_status
        if display_name:
            fila.display_name = display_name
        resultado = _resultado(fila, label_final, active, created)
    log.info(
        "twilio_conexion_alta",
        tenant_id=str(tenant_id),
        connection_id=str(connection_id),
        created=created,
        status=status,
        error_code=last_error,
    )
    return resultado


async def _registrar_webhooks(
    client: TwilioClient, sender_sid: str, inbound_url: str, status_url: str
) -> tuple[str, int | None, str, str]:
    """Escribe los webhooks del sender y los relee. Devuelve `(estado, código de error, estado del sender,
    display name)`: `connected` solo si Twilio quedó apuntando a las dos URL."""
    try:
        await set_webhooks(
            client, sender_sid, callback_url=inbound_url, status_callback_url=status_url
        )
        leido = await get_sender(client, sender_sid)
    except ProviderError as exc:
        log.warning("twilio_webhooks_no_registrados", code=exc.code, category=exc.category)
        return ("revoked" if exc.category == "AUTH" else "error"), exc.code, "", ""
    ok = (
        leido.webhook.get("callback_url") == inbound_url
        and leido.webhook.get("status_callback_url") == status_url
    )
    if not ok:
        log.warning("twilio_webhooks_no_coinciden", sender_sid=sender_sid)
    else:
        log.info("twilio_webhooks_registered", sender_sid=sender_sid)
    return ("connected" if ok else "error"), None, leido.status, leido.display_name


def _resultado(fila: TwilioConnection, label: str, active: bool, created: bool) -> ConexionTwilio:
    return ConexionTwilio(
        connection_id=fila.channel_connection_id,
        label=label,
        sender_e164=fila.sender_e164,
        sender_sid=fila.sender_sid,
        sender_status=fila.sender_status,
        display_name=fila.display_name,
        status=fila.status,
        account_sid=fila.account_sid,
        webhook_inbound_url=fila.webhook_inbound_url,
        webhook_status_url=fila.webhook_status_url,
        tenia_webhook_previo=bool(fila.previous_webhook),
        last_error_code=fila.last_error_code,
        active=active,
        created=created,
    )


async def obtener(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> ConexionTwilio | None:
    async with get_session() as session:
        row = (
            await session.execute(
                select(TwilioConnection, ChannelConnection)
                .join(
                    ChannelConnection,
                    (ChannelConnection.id == TwilioConnection.channel_connection_id)
                    & (ChannelConnection.tenant_id == TwilioConnection.tenant_id),
                )
                .where(
                    TwilioConnection.tenant_id == tenant_id,
                    TwilioConnection.channel_connection_id == connection_id,
                    TwilioConnection.deleted_at.is_(None),
                    ChannelConnection.deleted_at.is_(None),
                )
            )
        ).first()
    if row is None:
        return None
    fila, conexion = row
    return _resultado(fila, conexion.label, conexion.active, False)


async def credenciales_de_envio(
    session: Any, connection: ChannelConnection, creds: dict[str, str]
) -> dict[str, str]:
    """Credenciales que espera el adaptador de Twilio a partir de las guardadas en la conexión: agrega `from`
    (`whatsapp:+E164`) y `status_callback_url` (la URL registrada, de `twilio_connections`)."""
    fila = (
        await session.execute(
            select(TwilioConnection.webhook_status_url).where(
                TwilioConnection.tenant_id == connection.tenant_id,
                TwilioConnection.channel_connection_id == connection.id,
                TwilioConnection.deleted_at.is_(None),
            )
        )
    ).first()
    if fila is None:
        # Sin su fila viva (desconectada o a medio crear) la conexión no envía (ADR-0186): sin respaldo.
        return {}
    salida = {
        "account_sid": creds.get("account_sid", ""),
        "auth_token": creds.get("auth_token", ""),
        "from": f"whatsapp:{creds['sender_e164']}" if creds.get("sender_e164") else "",
    }
    if fila[0]:
        salida["status_callback_url"] = str(fila[0])
    return salida
