"""Operación de una conexión de WhatsApp vía Twilio (ADR-0186 §10-11, PR-5): salud, re-registro de webhooks, mensaje
de prueba, rotación de credenciales y desconexión.

Salud: la cuenta tiene que estar activa, el sender `ONLINE` y sus webhooks apuntando a nuestras URL. El estado de la
conexión queda en `twilio_connections.status`:

- `connected`: todo bien.
- `degraded`: el sender no está online o alguien cambió sus webhooks en la consola de Twilio.
- `revoked`: Twilio rechazó las credenciales (token rotado) o la cuenta no está activa.
- `error`: Twilio respondió con otro error. Una caída transitoria NO cambia el estado (no se alarma por un 503).

Al pasar a `degraded` o `revoked` se alerta una vez al día por conexión (`send_alert_once`). El cron
`core.twilio_health_check` (cada 15 min) revisa todas las conexiones vivas y activas de todos los negocios.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from agentcore_channel_twilio.adapters import TwilioOutbound
from agentcore_channel_twilio.client import TwilioClient
from agentcore_channel_twilio.senders import get_sender, restore_webhooks
from sqlalchemy import select, text

from core.db.models import ChannelConnection, TwilioConnection, utcnow
from core.db.session import get_session
from core.gateway.channels.base import ProviderError
from core.gateway.twilio import alta, sandbox
from core.shared.crypto import get_secret_box
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

_TEXTO_DE_PRUEBA = "Mensaje de prueba de solvershub: tu conexión de WhatsApp vía Twilio funciona."
# Mismos topes que el mensaje de prueba de Meta: uno cada 30 s y 10 al día por conexión.
_PRUEBA_CADA_S = 30
_PRUEBAS_POR_DIA = 10


@dataclass(frozen=True)
class _Conexion:
    connection: ChannelConnection
    fila: TwilioConnection
    creds: dict[str, Any]


async def _cargar(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> _Conexion | None:
    alta._exigir_scope(tenant_id)
    async with get_session() as session:
        par = (
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
    if par is None:
        return None
    connection, fila = par
    creds = (
        get_secret_box().decrypt_json(connection.credentials_encrypted)
        if connection.credentials_encrypted
        else {}
    )
    return _Conexion(connection, fila, creds)


def _cliente_de(c: _Conexion, transport: httpx.AsyncBaseTransport | None) -> TwilioClient:
    return alta._cliente(
        str(c.creds.get("account_sid") or ""), str(c.creds.get("auth_token") or ""), transport
    )


async def _guardar_estado(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    status: str | None,
    last_error_code: int | None = None,
    sender_status: str = "",
    display_name: str = "",
    desde: Any = None,
) -> str | None:
    """Persiste el resultado de una revisión sobre la fila releída con candado. Devuelve el estado anterior, o
    `None` si la fila cambió desde `desde` (una rotación de credenciales en medio): esa revisión ya no vale."""
    async with get_session() as session:
        fila = (
            await session.execute(
                select(TwilioConnection)
                .where(
                    TwilioConnection.tenant_id == tenant_id,
                    TwilioConnection.channel_connection_id == connection_id,
                    TwilioConnection.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if fila is None:
            return ""
        if desde is not None and fila.updated_at != desde:
            log.info(
                "twilio_salud_descartada_por_cambio",
                tenant_id=str(tenant_id),
                connection_id=str(connection_id),
            )
            return None
        anterior = fila.status
        if status is not None:
            fila.status = status
            fila.last_error_code = last_error_code
        if sender_status:
            fila.sender_status = sender_status
        if display_name:
            fila.display_name = display_name
        fila.last_health_at = utcnow()
    return anterior


async def _alertar(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, anterior: str, nuevo: str, motivo: str
) -> None:
    if nuevo not in ("degraded", "revoked") or anterior == nuevo:
        return
    from core.shared.alerts import send_alert_once

    await send_alert_once(
        f"twilio_{nuevo}:{connection_id}",
        "WhatsApp vía Twilio con problemas",
        f"Negocio {tenant_id}, conexión {connection_id}: {nuevo} ({motivo}).",
        severity="critical" if nuevo == "revoked" else "warning",
    )


async def revisar(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> alta.ConexionTwilio | None:
    """Revisa la cuenta, el sender y sus webhooks contra Twilio y guarda el estado. `None` si no existe."""
    c = await _cargar(tenant_id, connection_id)
    if c is None:
        return None
    if c.fila.modo == sandbox.MODO_SANDBOX:
        return await _revisar_sandbox(tenant_id, connection_id, c, transport)
    motivo = ""
    sender_status = display_name = ""
    codigo: int | None = None
    try:
        client = _cliente_de(c, transport)
        cuenta = await client.get(f"{client.account_url}.json", op="accounts.get")
        sender = await get_sender(client, c.fila.sender_sid or "")
    except alta.AltaError:
        nuevo, motivo = "revoked", "credenciales con formato inválido"
    except ProviderError as exc:
        codigo = exc.code
        if exc.category == "AUTH":
            nuevo, motivo = (
                "revoked",
                f"Twilio rechazó las credenciales ({exc.code or exc.http_status})",
            )
        elif exc.category in ("TRANSIENT", "AMBIGUOUS", "RATE_LIMIT", "QUOTA"):
            # Twilio no respondió: no se cambia el estado por una caída pasajera.
            await _guardar_estado(tenant_id, connection_id, status=None)
            log.info(
                "twilio_salud_sin_respuesta",
                tenant_id=str(tenant_id),
                connection_id=str(connection_id),
            )
            return await alta.obtener(tenant_id, connection_id)
        else:
            nuevo, motivo = "error", f"Twilio respondió {exc.code or exc.http_status}"
    else:
        sender_status, display_name = sender.status, sender.display_name
        if str(cuenta.get("status") or "").lower() != "active":
            nuevo, motivo = "revoked", "la cuenta de Twilio no está activa"
        elif not sender.online:
            nuevo, motivo = "degraded", f"el número está {sender.status}"
        elif not alta.apunta_a_nosotros(
            sender, c.fila.webhook_inbound_url, c.fila.webhook_status_url
        ):
            nuevo, motivo = "degraded", "los webhooks del número ya no apuntan a solvershub"
        else:
            nuevo = "connected"
    anterior = await _guardar_estado(
        tenant_id,
        connection_id,
        status=nuevo,
        last_error_code=codigo,
        sender_status=sender_status,
        display_name=display_name,
        desde=c.fila.updated_at,
    )
    if anterior is None:
        return await alta.obtener(tenant_id, connection_id)
    if anterior != nuevo:
        log.info(
            "twilio_sender_status_changed",
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
            antes=anterior,
            ahora=nuevo,
        )
    await _alertar(tenant_id, connection_id, anterior, nuevo, motivo)
    return await alta.obtener(tenant_id, connection_id)


async def _revisar_sandbox(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    c: _Conexion,
    transport: httpx.AsyncBaseTransport | None,
) -> alta.ConexionTwilio | None:
    """Salud de un sandbox (HU-CONN-20): no hay sender, así que solo la cuenta y la confirmación del webhook. Nunca
    llama a la Senders API. Con el gate apagado la conexión está inerte: no se consulta a Twilio."""
    if sandbox.inerte(c.fila.modo):
        return await alta.obtener(tenant_id, connection_id)
    motivo = ""
    codigo: int | None = None
    try:
        client = _cliente_de(c, transport)
        cuenta = await client.get(f"{client.account_url}.json", op="accounts.get")
    except alta.AltaError:
        nuevo, motivo = "revoked", "credenciales con formato inválido"
    except ProviderError as exc:
        codigo = exc.code
        if exc.category == "AUTH":
            nuevo, motivo = (
                "revoked",
                f"Twilio rechazó las credenciales ({exc.code or exc.http_status})",
            )
        elif exc.category in ("TRANSIENT", "AMBIGUOUS", "RATE_LIMIT", "QUOTA"):
            await _guardar_estado(tenant_id, connection_id, status=None)
            return await alta.obtener(tenant_id, connection_id)
        else:
            nuevo, motivo = "error", f"Twilio respondió {exc.code or exc.http_status}"
    else:
        if str(cuenta.get("status") or "").lower() != "active":
            nuevo, motivo = "revoked", "la cuenta de Twilio no está activa"
        elif c.fila.webhook_confirmado_at is None:
            # Las URL se pegan a mano en la consola: hasta el primer mensaje firmado no hay cómo saber si quedaron.
            nuevo = "pending"
        else:
            nuevo = "connected"
    anterior = await _guardar_estado(
        tenant_id,
        connection_id,
        status=nuevo,
        last_error_code=codigo,
        desde=c.fila.updated_at,
    )
    if anterior is None:
        return await alta.obtener(tenant_id, connection_id)
    if anterior != nuevo:
        log.info(
            "twilio_sender_status_changed",
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
            antes=anterior,
            ahora=nuevo,
            modo=sandbox.MODO_SANDBOX,
        )
    await _alertar(tenant_id, connection_id, anterior, nuevo, motivo)
    return await alta.obtener(tenant_id, connection_id)


def _error_webhook_manual(c: _Conexion) -> alta.AltaError:
    return alta.AltaError(
        409,
        "sandbox_webhook_manual",
        "Los webhooks del Sandbox solo se configuran en la consola de Twilio (Sandbox settings). Pega ahí estas dos "
        "direcciones.",
        {
            "webhook_inbound_url": c.fila.webhook_inbound_url,
            "webhook_status_url": c.fila.webhook_status_url,
        },
    )


async def revisar_todas() -> int:
    """Cron: revisa todas las conexiones vivas y activas. Una que falla no corta las demás. Devuelve cuántas."""
    async with get_session() as session:
        filas = (
            await session.execute(text("select id, tenant_id from admin_twilio_connection_ids()"))
        ).all()
    revisadas = sin_confirmar = 0
    for connection_id, tenant_id in filas:
        try:
            async with tenant_scope(tenant_id):
                revisada = await revisar(tenant_id, connection_id)
            revisadas += 1
            if revisada is not None and revisada.requiere_accion_manual:
                sin_confirmar += 1
        except Exception:
            log.exception(
                "twilio_salud_fallo", tenant_id=str(tenant_id), connection_id=str(connection_id)
            )
    sandbox.registrar_sin_confirmar(sin_confirmar)
    log.info(
        "twilio_salud_ronda",
        revisadas=revisadas,
        total=len(filas),
        sandbox_sin_confirmar=sin_confirmar,
    )
    return revisadas


async def reregistrar_webhooks(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> alta.ConexionTwilio | None:
    """Vuelve a escribir los webhooks del sender (con los overrides) y los relee."""
    c = await _cargar(tenant_id, connection_id)
    if c is None:
        return None
    if c.fila.modo == sandbox.MODO_SANDBOX or not c.fila.sender_sid:
        raise _error_webhook_manual(c)
    status, codigo, sender_status, display_name = await alta._registrar_webhooks(
        _cliente_de(c, transport),
        c.fila.sender_sid,
        c.fila.webhook_inbound_url,
        c.fila.webhook_status_url,
    )
    anterior = await _guardar_estado(
        tenant_id,
        connection_id,
        status=status,
        last_error_code=codigo,
        sender_status=sender_status,
        display_name=display_name,
    )
    await _alertar(tenant_id, connection_id, anterior, status, "re-registro de webhooks")
    return await alta.obtener(tenant_id, connection_id)


async def enviar_prueba(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    to: str,
    *,
    actor: str = "admin",
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Manda un texto de prueba al número que indica el negocio. Devuelve el SID del mensaje. WhatsApp solo deja
    mandar texto libre dentro de la ventana de 24 h: el destinatario tiene que haberle escrito antes al número."""
    from agentcore_channel_twilio.adapters import whatsapp_address

    from core.memory.working import get_redis

    c = await _cargar(tenant_id, connection_id)
    if c is None or sandbox.inerte(c.fila.modo):
        raise alta.AltaError(404, "not_found", "Conexión no encontrada.")
    if not c.connection.active:
        raise alta.AltaError(409, "conexion_pausada", "La conexión está pausada.")
    try:
        destino = whatsapp_address(to)
    except ProviderError:
        raise alta.AltaError(
            422, "numero_invalido", "Ese número no es válido. Usa el formato +57300…"
        ) from None
    redis = get_redis()
    if not await redis.set(
        f"twilio_prueba:{tenant_id}:{connection_id}", "1", nx=True, ex=_PRUEBA_CADA_S
    ):
        raise alta.AltaError(
            429, "too_many_tests", "Espera unos segundos antes de mandar otra prueba."
        )
    clave_dia = f"twilio_prueba_dia:{tenant_id}:{connection_id}"
    # El TTL se fija al crear la clave (SET NX EX) y nunca queda sin él aunque falle algo después.
    await redis.set(clave_dia, 0, nx=True, ex=86400)
    usados = await redis.incr(clave_dia)
    if usados > _PRUEBAS_POR_DIA:
        raise alta.AltaError(429, "daily_limit", "Ya mandaste las pruebas de hoy.")

    async with get_session() as session:
        creds = await alta.credenciales_de_envio(
            session, c.connection, {k: str(v) for k, v in c.creds.items()}
        )
    try:
        sid = await TwilioOutbound(transport).send_text(creds, destino, _TEXTO_DE_PRUEBA)
    except ProviderError as exc:
        if exc.code == sandbox.CODIGO_NO_UNIDO:
            log.info(
                "twilio_sandbox_no_unido",
                tenant_id=str(tenant_id),
                connection_id=str(connection_id),
                via="prueba",
            )
            sandbox.contar("no_unido")
            raise _no_unido(c) from None
        if exc.category == "WINDOW":
            raise alta.AltaError(
                409,
                "fuera_de_ventana",
                "WhatsApp solo deja mandar texto si ese número te escribió en las últimas 24 h. Escríbele primero al "
                "número del negocio desde ese teléfono y vuelve a probar.",
            ) from None
        raise alta._traducir(exc) from None
    from core.shared.audit import record_audit

    await record_audit(
        actor,
        "twilio.test_message",
        input_data={"connection_id": str(connection_id), "to_last4": destino[-4:]},
    )
    return sid


def _no_unido(c: _Conexion) -> alta.AltaError:
    """63015: el destinatario no se unió al sandbox de la cuenta, o su unión de 3 días venció."""
    codigo = c.fila.sandbox_join_code
    union = f"join {codigo}" if codigo else "join <código de tu sandbox>"
    numero = c.fila.sender_e164
    return alta.AltaError(
        422,
        "participante_no_unido",
        f"Ese número no está unido al Sandbox de Twilio. Desde ese teléfono, envía «{union}» por WhatsApp al "
        f"{numero} y vuelve a probar. La unión vence a los 3 días.",
        {"join": union, "sandbox_e164": numero},
    )


async def rotar_credenciales(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    account_sid: str,
    auth_token: str,
    confirm_overwrite_webhook: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> alta.ConexionTwilio:
    """Cambia el Auth Token (o la cuenta) de la conexión y vuelve a registrar los webhooks. Es el alta de nuevo con
    el mismo número: valida contra Twilio y no toca la conexión si las credenciales nuevas no sirven."""
    c = await _cargar(tenant_id, connection_id)
    if c is None:
        raise alta.AltaError(404, "not_found", "Conexión no encontrada.")
    if not c.connection.active:
        # El alta revive y activa la conexión: rotar una pausada la pondría a atender sin que nadie lo pidiera.
        raise alta.AltaError(
            409, "conexion_pausada", "Reanuda la conexión antes de cambiar sus credenciales."
        )
    if c.fila.modo == sandbox.MODO_SANDBOX:
        if account_sid.strip() != c.fila.account_sid:
            # El sandbox es de la cuenta: otra cuenta es otro sandbox (su propia conexión y sus propias URL).
            raise alta.AltaError(
                422,
                "sandbox_otra_cuenta",
                "Este sandbox es de otra cuenta de Twilio. Para usar otra cuenta, conecta su sandbox aparte.",
            )
        # Revalida la cuenta y guarda el token nuevo en la misma conexión (mismo `public_id`, mismo código).
        return await alta.conectar_sandbox(
            tenant_id,
            account_sid=account_sid,
            auth_token=auth_token,
            label=c.connection.label,
            transport=transport,
        )
    return await alta.conectar(
        tenant_id,
        account_sid=account_sid,
        auth_token=auth_token,
        sender_sid=c.fila.sender_sid or "",
        label=c.connection.label,
        # Si alguien movió los webhooks a otra integración, se pregunta igual que en el alta.
        confirm_overwrite_webhook=confirm_overwrite_webhook,
        transport=transport,
    )


async def desconectar(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    restaurar_webhooks: bool,
    continuar_si_falla: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Desconecta el número: primero (si se pide) le devuelve al sender los webhooks que tenía antes y después borra
    lógicamente la conexión y su fila de Twilio (libera el número). Restaurar va primero para poder reintentar: si
    falla y no se pidió `continuar_si_falla`, no se borra nada (502 `webhooks_no_restaurados`). Solo se restaura si
    el sender sigue apuntando a solvershub: si el dueño ya lo movió a otra integración, no se toca. Devuelve si
    restauró."""
    from core.gateway.connections import delete_connection

    c = await _cargar(tenant_id, connection_id)
    if c is None:
        raise alta.AltaError(404, "not_found", "Conexión no encontrada.")
    restaurados = False
    # El sandbox no tiene webhooks que restaurar por API: sus URL las borra el dueño en la consola de Twilio.
    if restaurar_webhooks and c.fila.modo != sandbox.MODO_SANDBOX and c.fila.sender_sid:
        try:
            client = _cliente_de(c, transport)
            sender_sid = c.fila.sender_sid
            sender = await get_sender(client, sender_sid)
            if alta.apunta_a_nosotros(
                sender, c.fila.webhook_inbound_url, c.fila.webhook_status_url
            ):
                await restore_webhooks(client, sender_sid, dict(c.fila.previous_webhook or {}))
                restaurados = True
        except (ProviderError, alta.AltaError):
            log.warning(
                "twilio_webhooks_no_restaurados",
                tenant_id=str(tenant_id),
                connection_id=str(connection_id),
            )
            if not continuar_si_falla:
                raise alta.AltaError(
                    502,
                    "webhooks_no_restaurados",
                    "No se pudieron devolver los webhooks anteriores al número. Intenta de nuevo, o desconecta sin "
                    "restaurarlos.",
                ) from None
    await delete_connection(connection_id)
    if c.fila.modo == sandbox.MODO_SANDBOX:
        sandbox.contar("desconectado")
        log.info(
            "twilio_sandbox_desconectado",
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
        )
    log.info(
        "twilio_conexion_desconectada",
        tenant_id=str(tenant_id),
        connection_id=str(connection_id),
        restaurados=restaurados,
    )
    return restaurados
