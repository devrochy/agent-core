"""Reconciliación de entrantes de WhatsApp vía Twilio (adenda 2 del ADR-0186).

El webhook es la vía normal, pero un mensaje que no llega a nuestra API se pierde para siempre, porque Twilio solo
reintenta unos segundos (`#rc=3`). Le pasa a un entrante que llega:
- en pleno despliegue: la API tarda en levantar y Twilio recibe 502;
- con el túnel caído;
- a una URL vieja: el 2026-11-19, cuatro entrantes de producción fueron a la conexión pausada de test.

El cron lee de la API de Twilio los entrantes del número de una ventana corta y publica los que no tienen fila de
auditoría, por el mismo camino que el webhook (`ingerir_entrante`). La idempotencia es la de siempre:
`claim_message_once` por `MessageSid`, `ProcessedEvent` y el UNIQUE de `messages`. Un mensaje que llega a la vez
por las dos vías entra una sola vez.

Límites (el recurso Message no es el formulario del webhook):
- Sin `ProfileName`: el cliente nuevo nace sin nombre.
- Sin `ButtonPayload`: el toque de un botón llega como su texto (`Body`) y sin el id.
- La media sale del subrecurso `Media`.
- Llega tarde (hasta unos minutos) y puede quedar después de mensajes más nuevos del mismo cliente. Se registra
  la demora en el log, no se reordena.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from agentcore_channel_twilio.messages import list_inbound_messages, list_media
from agentcore_channel_twilio.senders import get_sender
from sqlalchemy import text

from core.db.session import get_session
from core.gateway.channels.base import ProviderError
from core.gateway.twilio import alta, sandbox
from core.gateway.twilio.operacion import _cargar, _cliente_de
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

# Ventana: desde hace 30 min hasta hace 90 s. El margen deja al webhook llegar primero; 30 min cubre varias
# corridas del cron (cada 2 min) y el corte de un despliegue con holgura, sin recorrer el historial.
VENTANA = timedelta(minutes=30)
MARGEN = timedelta(seconds=90)
_AVISO_CADA_S = 3600


def _fecha(valor: str) -> datetime | None:
    """`date_created` del recurso (RFC 2822, `Thu, 20 Nov 2026 02:10:23 +0000`)."""
    try:
        fecha = parsedate_to_datetime(valor)
    except (TypeError, ValueError):
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=UTC)


async def reconciliar(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    ahora: datetime | None = None,
) -> int:
    """Publica los entrantes de la ventana que no llegaron por el webhook. Devuelve cuántos recuperó. Solo actúa
    sobre un sender conectado y activo, y solo publica después de confirmar en vivo que el número apunta a ESTE
    ambiente: así un ambiente no le contesta a los clientes de otro."""
    from core.billing.enforcement import is_tenant_operational
    from core.gateway.twilio.webhook import ingerir_entrante
    from core.gateway.webhooks import webhook_events_existentes

    c = await _cargar(tenant_id, connection_id)
    if c is None or not c.connection.active:
        return 0
    if c.fila.modo == sandbox.MODO_SANDBOX or c.fila.status != "connected":
        return 0
    sender = c.fila.sender_e164
    if not sender or not c.fila.sender_sid or not await is_tenant_operational(tenant_id):
        return 0
    if str(c.creds.get("account_sid") or "") != c.fila.account_sid:
        # Credenciales a medio rotar: la consulta y la auditoría apuntarían a cuentas distintas.
        log.info("twilio_reconciliacion_cuenta_distinta", tenant_id=str(tenant_id))
        return 0

    ahora = ahora or datetime.now(UTC)
    desde, hasta = ahora - VENTANA, ahora - MARGEN
    client = _cliente_de(c, transport)
    try:
        entrantes = await list_inbound_messages(
            client, f"whatsapp:{sender}", desde=desde.strftime("%Y-%m-%d")
        )
    except ProviderError as exc:
        # Twilio inalcanzable (la red del host cayó horas el 2026-11-20): la próxima corrida lo intenta de nuevo.
        log.info(
            "twilio_reconciliacion_sin_respuesta",
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
            category=exc.category,
        )
        return 0

    candidatos = []
    for m in entrantes:
        fecha = _fecha(m.date_created)
        if m.sid and fecha is not None and desde <= fecha <= hasta:
            candidatos.append((m, fecha))
    if not candidatos:
        return 0
    app_id = f"twilio:{c.fila.account_sid}"
    vistos = await webhook_events_existentes(app_id, tenant_id, [m.sid for m, _ in candidatos])
    faltan = [(m, fecha) for m, fecha in candidatos if m.sid not in vistos]
    if not faltan:
        return 0
    # Antes de publicar se confirma EN VIVO que el número apunta a este ambiente. El `status` guardado lo refresca la
    # salud cada 15 min: si el número se movió a otro ambiente (test↔producción con la misma cuenta), este todavía
    # diría `connected` y le contestaría a clientes ajenos con datos que no son suyos. Sin confirmación, nada.
    try:
        en_vivo = await get_sender(client, c.fila.sender_sid)
    except ProviderError as exc:
        log.info(
            "twilio_reconciliacion_sin_respuesta",
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
            category=exc.category,
        )
        return 0
    if not alta.apunta_a_nosotros(en_vivo, c.fila.webhook_inbound_url, c.fila.webhook_status_url):
        log.warning(
            "twilio_reconciliacion_numero_ajeno",
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
            pendientes=len(faltan),
        )
        return 0
    public_id = c.fila.public_id
    recuperados = 0
    # Del más viejo al más nuevo: si faltan varios del mismo cliente, entran en su orden.
    for m, fecha in sorted(faltan, key=lambda par: par[1]):
        params = {
            "MessageSid": m.sid,
            "AccountSid": c.fila.account_sid,
            "From": m.from_,
            "To": m.to,
            "Body": m.body,
            "NumMedia": str(m.num_media),
            "SmsStatus": "received",
        }
        if m.num_media:
            try:
                adjuntos = await list_media(client, m.sid)
            except ProviderError:
                # Sin la lista de adjuntos no se publica a medias: la próxima corrida lo reintenta.
                log.info("twilio_reconciliacion_media_sin_respuesta", tenant_id=str(tenant_id))
                continue
            for n, (url, mime) in enumerate(adjuntos):
                params[f"MediaUrl{n}"] = url
                params[f"MediaContentType{n}"] = mime
            params["NumMedia"] = str(len(adjuntos))
        resultado = await ingerir_entrante(
            tenant_id, connection_id, c.fila, params, public_id, via="reconciliacion"
        )
        if resultado != "enqueued":
            # `duplicate` (ya había entrado), `dropped` (cuota de medios) o `sin_auditoria`: no es una recuperación.
            log.info(
                "twilio_reconciliacion_no_publicado", tenant_id=str(tenant_id), resultado=resultado
            )
            continue
        recuperados += 1
        log.warning(
            "twilio_entrante_reconciliado",
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
            resultado=resultado,
            demora_s=round((ahora - fecha).total_seconds()),
        )
    if recuperados:
        from core.shared.alerts import send_alert_once

        await send_alert_once(
            f"twilio_entrantes_reconciliados:{connection_id}",
            "WhatsApp vía Twilio: entrantes recuperados",
            f"Negocio {tenant_id}, conexión {connection_id}: {recuperados} mensaje(s) de clientes no llegaron por el "
            "webhook y se recuperaron de la API de Twilio. Revisa el túnel, los despliegues recientes y a dónde "
            "apunta el número.",
            window_s=_AVISO_CADA_S,
        )
    return recuperados


async def reconciliar_todas() -> int:
    """Cron: reconcilia cada conexión viva y activa. Una que falla no corta las demás. Devuelve cuántos recuperó."""
    async with get_session() as session:
        filas = (
            await session.execute(text("select id, tenant_id from admin_twilio_connection_ids()"))
        ).all()
    total = 0
    for connection_id, tenant_id in filas:
        try:
            async with tenant_scope(tenant_id):
                total += await reconciliar(tenant_id, connection_id)
        except Exception:
            log.exception(
                "twilio_reconciliacion_fallo",
                tenant_id=str(tenant_id),
                connection_id=str(connection_id),
            )
    if total:
        log.info("twilio_reconciliacion_ronda", conexiones=len(filas), recuperados=total)
    return total
