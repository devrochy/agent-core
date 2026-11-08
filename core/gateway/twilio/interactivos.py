"""Botones y listas por WhatsApp vía Twilio (ADR-0186 §9, PR-7i).

El kernel arma el interactivo igual que para Meta (`{"type": "button", "body", "buttons": [{id, title}]}` o
`{"type": "list", "body", "button", "rows": [{id, title, description?}]}`, ya saneado a los límites de WhatsApp).
Aquí se convierte en un Content de Twilio por FORMA, con todo en variables (Twilio las admite en el cuerpo, los
títulos y los ids de un mensaje dentro de la ventana de 24 h):

- `quick_reply:N` → `{{1}}` el cuerpo y, por cada botón k, `{{2k}}` el título y `{{2k+1}}` el id.
- `list:N` → `{{1}}` el cuerpo, `{{2}}` el botón y, por cada fila k, `{{3k}}` el título, `{{3k+1}}` el id y
  `{{3k+2}}` la descripción (Twilio la exige: si la fila no trae, va un guion).

Los títulos, ids y descripciones van sin saltos ni caracteres de control; un título vacío o un id más largo que el
tope de Twilio es un `ChannelError` (no se trunca: el id tiene que volver igual en `ButtonPayload`/`ListId`).

El Content se crea la primera vez y se cachea en `twilio_interactive_contents` con la cuenta que lo creó: si la
conexión cambia de cuenta (rotar credenciales, reconectar el número con otra), la fila vieja se reemplaza. Si alguien
lo borró en la consola, se recrea una vez. El pie (`footer`) se agrega al cuerpo. Sin texto de respaldo aquí (como en Meta): un interactivo
que Twilio no acepta falla y se escala; quien puede degradar a texto ya lo decidió al encolar (`sender`).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
from agentcore_channel_twilio.adapters import TwilioOutbound
from agentcore_channel_twilio.client import TwilioClient
from agentcore_channel_twilio.content import create_content
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db.models import TwilioInteractiveContent, utcnow
from core.db.session import get_session
from core.gateway.channels.base import ProviderError
from core.shared.exceptions import ChannelError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

MAX_CUERPO = 1024
MAX_BOTONES = 3  # quick-reply en sesión, sin aprobación
MAX_FILAS = 10
MAX_TITULO_BOTON = 20
MAX_TITULO_FILA = 24
MAX_DESCRIPCION = 72
MAX_ID = 200
_SIN_DESCRIPCION = "-"
_SID_HX = re.compile(r"HX[0-9a-f]{32}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_CONTROL_SIN_SALTO = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]+")


def _linea(valor: Any, tope: int) -> str:
    """Una línea: sin saltos ni caracteres de control, sin espacios de más, acotada."""
    texto = re.sub(r" {2,}", " ", _CONTROL.sub(" ", str(valor or ""))).strip()
    return texto[:tope].rstrip()


def _titulo(valor: Any, tope: int) -> str:
    texto = _linea(valor, tope)
    if not texto:
        raise ChannelError("Opción sin título")
    return texto


def _id(valor: Any) -> str:
    texto = _CONTROL.sub("", str(valor or "")).strip()
    if not texto or len(texto) > MAX_ID:
        raise ChannelError("Id de opción vacío o más largo de lo que Twilio admite")
    return texto


def _cuerpo(interactive: dict[str, Any]) -> str:
    def limpio(v: Any) -> str:
        return _CONTROL_SIN_SALTO.sub(" ", str(v or "")).strip()

    cuerpo, pie = limpio(interactive.get("body")), limpio(interactive.get("footer"))
    if pie:
        cuerpo = f"{cuerpo}\n\n{pie}"
    return cuerpo[:MAX_CUERPO].rstrip()


def forma_y_variables(interactive: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, str]]:
    """`(forma, types del Content, variables del envío)`. Lanza `ChannelError` si el interactivo no sirve."""
    tipo = interactive.get("type")
    cuerpo = _cuerpo(interactive)
    if not cuerpo:
        raise ChannelError("Mensaje interactivo sin cuerpo")
    variables: dict[str, str] = {"1": cuerpo}
    if tipo == "button":
        botones = list(interactive.get("buttons") or [])
        if not 1 <= len(botones) <= MAX_BOTONES:
            raise ChannelError(f"Twilio admite de 1 a {MAX_BOTONES} botones en sesión")
        acciones = []
        for k, b in enumerate(botones, start=1):
            variables[str(2 * k)] = _titulo(b.get("title"), MAX_TITULO_BOTON)
            variables[str(2 * k + 1)] = _id(b.get("id"))
            acciones.append({"type": "QUICK_REPLY", "title": f"{{{{{2 * k}}}}}", "id": f"{{{{{2 * k + 1}}}}}"})
        tipos = {"twilio/quick-reply": {"body": "{{1}}", "actions": acciones}}
        return f"quick_reply:{len(botones)}", tipos, variables
    if tipo == "list":
        filas = list(interactive.get("rows") or [])
        if not 1 <= len(filas) <= MAX_FILAS:
            raise ChannelError(f"Twilio admite de 1 a {MAX_FILAS} filas en una lista")
        variables["2"] = _titulo(interactive.get("button"), MAX_TITULO_BOTON)
        items = []
        for k, f in enumerate(filas, start=1):
            variables[str(3 * k)] = _titulo(f.get("title"), MAX_TITULO_FILA)
            variables[str(3 * k + 1)] = _id(f.get("id"))
            variables[str(3 * k + 2)] = _linea(f.get("description"), MAX_DESCRIPCION) or _SIN_DESCRIPCION
            items.append(
                {"item": f"{{{{{3 * k}}}}}", "id": f"{{{{{3 * k + 1}}}}}", "description": f"{{{{{3 * k + 2}}}}}"}
            )
        tipos = {"twilio/list-picker": {"body": "{{1}}", "button": "{{2}}", "items": items}}
        return f"list:{len(filas)}", tipos, variables
    raise ChannelError(f"Tipo de interactivo desconocido: {tipo!r}")


def _de_la_forma(tenant_id: uuid.UUID, connection_id: uuid.UUID, forma: str) -> Any:
    return select(TwilioInteractiveContent).where(
        TwilioInteractiveContent.tenant_id == tenant_id,
        TwilioInteractiveContent.channel_connection_id == connection_id,
        TwilioInteractiveContent.forma == forma,
        TwilioInteractiveContent.deleted_at.is_(None),
    )


async def _content_de(
    client: TwilioClient,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    forma: str,
    tipos: dict[str, Any],
    variables: dict[str, str],
) -> str:
    async with get_session() as session:
        fila = (await session.execute(_de_la_forma(tenant_id, connection_id, forma))).scalar_one_or_none()
        if fila is not None and fila.account_sid == client.account_sid:
            fila.last_used_at = utcnow()
            return fila.content_sid
        if fila is not None:
            # De otra cuenta (la conexión rotó credenciales o se reconectó con otra): no sirve con esta.
            fila.deleted_at = utcnow()
    # Ejemplos neutros: el Content queda en la cuenta del negocio y no debe guardar datos del primer cliente.
    ejemplos = {k: f"v{k}" for k in variables}
    content = await create_content(
        client,
        friendly_name=f"solvershub_{forma.replace(':', '_')}_{uuid.uuid4().hex[:8]}",
        language="es",
        types=tipos,
        variables=ejemplos,
    )
    if not _SID_HX.fullmatch(content.sid):
        raise ProviderError("Twilio no devolvió el ContentSid", provider="twilio", category="AMBIGUOUS")
    async with get_session() as session:
        await session.execute(
            pg_insert(TwilioInteractiveContent)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                channel_connection_id=connection_id,
                account_sid=client.account_sid,
                forma=forma,
                content_sid=content.sid,
                last_used_at=utcnow(),
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "channel_connection_id", "forma"],
                index_where=TwilioInteractiveContent.deleted_at.is_(None),
            )
        )
        # Si otro envío ganó la carrera, se usa el suyo (el recién creado queda sin usar en la cuenta).
        ganadora = (await session.execute(_de_la_forma(tenant_id, connection_id, forma))).scalar_one_or_none()
    log.info("twilio_interactivo_creado", tenant_id=str(tenant_id), forma=forma)
    if ganadora is not None and ganadora.account_sid == client.account_sid:
        return ganadora.content_sid
    return content.sid


async def _olvidar(tenant_id: uuid.UUID, connection_id: uuid.UUID, forma: str, content_sid: str) -> None:
    """Da de baja la fila de ESE Content: si otro envío ya lo recreó, la fila nueva no se toca."""
    async with get_session() as session:
        await session.execute(
            update(TwilioInteractiveContent)
            .where(
                TwilioInteractiveContent.tenant_id == tenant_id,
                TwilioInteractiveContent.channel_connection_id == connection_id,
                TwilioInteractiveContent.forma == forma,
                TwilioInteractiveContent.content_sid == content_sid,
                TwilioInteractiveContent.deleted_at.is_(None),
            )
            .values(deleted_at=utcnow())
        )


async def enviar(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    credentials: dict[str, str],
    to_external_id: str,
    interactive: dict[str, Any],
    *,
    outbound: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Manda el interactivo por su Content de forma. Devuelve el SID del mensaje. Si Twilio no encuentra el Content
    cacheado (borrado en la consola), lo recrea una vez. `outbound`: el adaptador del registry (el mismo que manda
    las plantillas); sin él, uno de Twilio. Lanza `ProviderError` o `ChannelError`."""
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("interactivo de Twilio: fuera del tenant_scope del negocio")
    forma, tipos, variables = forma_y_variables(interactive)
    client = TwilioClient.from_credentials(credentials, transport=transport)
    outbound = outbound or TwilioOutbound(transport)
    for intento in (1, 2):
        sid = await _content_de(client, tenant_id, connection_id, forma, tipos, variables)
        try:
            return await outbound.send_content_template(credentials, to_external_id, sid, variables)
        except ProviderError as exc:
            # Solo «no existe» recrea: un CONTENT por otra causa (variables) fallaría igual con uno nuevo. El código
            # real de un ContentSid borrado se confirma en test con el número (ADR-0186, adenda).
            if intento == 1 and (exc.http_status == 404 or exc.code == 20404):
                log.warning("twilio_interactivo_recreado", tenant_id=str(tenant_id), forma=forma, code=exc.code)
                await _olvidar(tenant_id, connection_id, forma, sid)
                continue
            raise
    raise ChannelError("No se pudo enviar el interactivo")  # pragma: no cover - el bucle siempre retorna o lanza
