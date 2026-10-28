"""Puente aviso por evento → plantilla con opciones (ADR-0178 D3, une ADR-0108 con ADR-0172).

Un negocio puede apuntar un evento (`message_templates_map[<evento>]["option_template"]`) a la clave de una plantilla
con opciones PUBLICADA. El aviso sale entonces con sus botones, su lista o su menú numerado según el canal, por la
cascada que ya tiene el envío de opciones (`opciones_envio.enviar_opciones`, pistas C1/C2): interactivo con la ventana
de 24 h abierta, plantilla de Meta aprobada (`<clave>_v<n>`) con la ventana cerrada, texto numerado en QR, chat web,
Messenger e Instagram. El menú ES el aviso (no se manda además el texto de respaldo), así que es el último saliente y
el «1» del cliente lo resuelve el consumer sin LLM (adenda C2): D3 es el primer emisor FUERA DE TURNO de C2.

**Cuándo se intenta** (todo debe cumplirse; si no, `send_event_notice` sigue EXACTAMENTE como antes):

1. la bandera `avisos_por_canal` del negocio (la lee quien llama, `por_canal=True`);
2. la bandera `opciones_directas` del negocio (se lee aquí ANTES de enviar: apagada no deja ni un `option_send` en la
   auditoría por cada aviso);
3. el evento tiene `option_template` con una clave bien formada y NO es de cobranza (abajo);
4. el canal puede recibirlo ahora: la MISMA regla de D2 (`avisos_canal.bloqueo_de_canal`: ventana de 24 h en
   Messenger/Instagram) y, en QR, la política anti-baneo (`waweb_bloqueo`: modo, tope diario, pausa). WhatsApp Meta
   siempre se intenta y la cascada decide la vía. WhatsApp por Twilio/Plivo, correo y el playground no entran.

**Si no sale** (plantilla borrada o sin publicar, contexto incompleto, sin plantilla de Meta aprobada, cola que
rechaza, error): se registra `event_notice_opciones_fallback` con el `outcome` y el `motivo` (códigos, nunca valores) y
se cae al comportamiento de D2/legacy sin dejar un mensaje `failed` de más (`sin_mensaje_de_fallo`). Un
`DUPLICADO` (el outbox reintenta un aviso ya enviado) NO cae al texto: sería un segundo aviso.

**Cobranza fuera del puente (Ley 2300, HU-SUB-18).** Los tres eventos de `COBRANZA` no se mapean a plantillas con
opciones, en ningún canal. Aquí el aviso no solo tiene que EMITIRSE en horario legal (lo decide `pack_subscriptions`):
tiene que ENTREGARSE en él, y el puente suma caminos que no lo garantizan (cola diferida de QR, reintentos del outbox
con backoff, reenvío manual del panel). Mientras no haya una garantía de horario en la entrega, esos eventos conservan
su camino de siempre. El PUT de Configuración lo rechaza y este módulo lo ignora si el mapa se guardó a mano.

**El contexto lo arma el servidor** (`datos_del_evento`): del contexto que el handler construye del evento y de la
base, restringido a las rutas de ese evento que además están en `RUTAS_DE_DATOS`. Nunca del texto del cliente ni del
LLM. Ninguna ruta es un id (una prueba lo fija), así que no hay un id de otro negocio o de otro cliente que pueda
llegar a un argumento de tool; la conversación sale del cliente del evento, dentro del negocio. Las opciones usan el
payload firmado `opt:` del envío, el mismo resolver fail-closed y las mismas guardas de C2 (las sensibles no se
ejecutan por texto): este módulo no toca nada de eso.

Borde: si `opciones_directas` se apaga ENTRE el primer envío de un aviso y un reintento del outbox, el reintento
cae al texto de D2 y el cliente recibiría dos avisos. Es una ventana de segundos y apagar la bandera es un acto del
master; se acepta y queda dicho.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from opentelemetry import metrics
from sqlalchemy import func, select

from core.db.models import Conversation, Message, utcnow
from core.db.session import get_session
from core.gateway import avisos_canal as ac
from core.gateway.connection_types import capacidades_de, tipo_de_conexion
from core.kernel.mensajes import COBRANZA, EVENTOS, SIN_OPCIONES, valor_de_ruta
from core.kernel.opciones import bandera, contexto
from core.kernel.opciones import envio as ev
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

if TYPE_CHECKING:
    from core.gateway.event_notices import NoticeResult

log = get_logger(__name__)

# Clave del mapa del negocio y su forma (la misma de `option_templates.key`, `ck_option_templates_key`).
CLAVE_DEL_MAPA = "option_template"
FORMA_DE_CLAVE = re.compile(r"^[a-z0-9_]{1,40}$")

# Motivos de este módulo (el resto sale del envío de opciones o de `avisos_canal`; conjunto cerrado para el log).
CANAL_SIN_PUENTE = "canal_sin_puente"
OPCIONES_APAGADAS = "opciones_directas_apagadas"
SIN_VIA = "sin_via"
CONVERSACION_ACTIVA = "conversacion_activa"

# Un menú de texto fuera de turno se vuelve el último saliente: si el cliente venía hablando con el agente, su próximo
# «1» (que era una respuesta a otra pregunta) lo ejecutaría el consumer como la opción 1 del menú (adenda C2). Con
# actividad reciente en la conversación NO se manda el menú de texto: cae al texto de D2.
VENTANA_DE_ACTIVIDAD = timedelta(minutes=15)

_meter = metrics.get_meter("agent-core")
_fallbacks = _meter.create_counter(
    "event_notice_options_fallback_total",
    description="Avisos por evento mapeados a una plantilla con opciones que no salieron como tal, por motivo",
)


def clave_de_opciones(evento: str, entrada: Any) -> str | None:
    """La clave de la plantilla con opciones que el negocio mapeó al evento, o None si no hay, está mal formada o el
    evento no puede usar el puente (desconocido o de cobranza). Pura."""
    valor = entrada.get(CLAVE_DEL_MAPA) if isinstance(entrada, dict) else None
    if evento not in EVENTOS or evento in SIN_OPCIONES or not isinstance(valor, str):
        return None
    valor = valor.strip()
    return valor if FORMA_DE_CLAVE.fullmatch(valor) else None


def datos_del_evento(evento: str, ctx: dict[str, Any]) -> dict[str, str]:
    """`ruta → texto` de los datos del evento que una plantilla con opciones puede citar: las rutas de ESE evento que
    además están en `RUTAS_DE_DATOS` (la base, `cliente.nombre` y `negocio.nombre`, la resuelve el envío desde la
    conversación). Cada valor pasa por `clean_param`; un hueco o un valor no simple deja la ruta sin resolver (si la
    plantilla la necesita, no se envía). Pura."""
    datos: dict[str, str] = {}
    if evento in SIN_OPCIONES:
        return datos
    for ruta in EVENTOS.get(evento, {}):
        if ruta in contexto.RUTAS_DE_DATOS and (valor := valor_de_ruta(ctx, ruta)) is not None:
            datos[ruta] = valor
    return datos


async def _opciones_habilitadas(tenant_id: uuid.UUID) -> bool:
    """La bandera `opciones_directas` del negocio. Fail-closed: si no se puede leer, apagada."""
    try:
        async with get_session() as session:
            return await bandera.habilitada(session, tenant_id)
    except Exception:
        log.warning("event_notice_opciones_bandera_ilegible", exc_info=True)
        return False


def _cae(evento: str, conversation_id: uuid.UUID, canal: str, outcome: str, motivo: str = "") -> None:
    """Rastro de un puente que no salió y cae al camino de D2/legacy: log y métrica, códigos y nunca valores."""
    _fallbacks.add(1, {"canal": canal[:20], "outcome": outcome[:40]})
    log.info(
        "event_notice_opciones_fallback",
        tenant_id=str(get_current_tenant_id()),
        conversation_id=str(conversation_id),
        evento=evento,
        canal=canal,
        outcome=outcome,
        motivo=motivo,
    )


async def _conversacion_activa(conversation_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    """Hubo un mensaje (entrante o saliente que no falló) en la conversación dentro de `VENTANA_DE_ACTIVIDAD`."""
    async with get_session() as session:
        ultimo = (
            await session.execute(
                select(func.max(Message.created_at)).where(
                    Message.tenant_id == tenant_id, Message.conversation_id == conversation_id, Message.status != "failed"
                )
            )
        ).scalar_one_or_none()
    return ultimo is not None and ultimo >= utcnow() - VENTANA_DE_ACTIVIDAD


async def intentar(
    conversation: Conversation, evento: str, ctx: dict[str, Any], notice_key: str, entrada: dict[str, Any]
) -> NoticeResult | None:
    """Envía el aviso como plantilla con opciones si se puede. Devuelve el `NoticeResult` si este camino lo resolvió
    (salió, o ya había salido) o None para seguir con el camino de D2/legacy. Corre dentro del `tenant_scope` del
    evento y NUNCA lanza hacia `send_event_notice` por un fallo del puente: cualquier error es un fallback."""
    from core.gateway.event_notices import NoticeNotSentError, NoticeResult
    from core.gateway.opciones_envio import enviar_opciones

    clave = clave_de_opciones(evento, entrada)
    if clave is None:
        return None
    tenant_id = conversation.tenant_id
    if not await _opciones_habilitadas(tenant_id):
        return None  # apagada: sin rastro, el camino es el de siempre

    canal = conversation.channel
    proveedor = await ac.proveedor_de_conversacion(conversation)
    tipo = tipo_de_conexion(canal, proveedor)
    if tipo not in ac.TIPOS_DE_TEXTO and not capacidades_de(canal, proveedor).plantilla_aprobada:
        _cae(evento, conversation.id, canal, CANAL_SIN_PUENTE)
        return None
    expira = conversation.service_window_expires_at
    if motivo := ac.bloqueo_de_canal(tipo, evento, expira is not None and expira >= utcnow()):
        _cae(evento, conversation.id, canal, SIN_VIA, motivo)
        return None
    if tipo == ac.QR and (motivo := await ac.waweb_bloqueo(tenant_id)):
        _cae(evento, conversation.id, canal, SIN_VIA, motivo)
        return None

    async def _guarda_de_turno() -> str:
        # Corre DENTRO del envío, después de la idempotencia: el menú de un envío anterior de este mismo aviso cuenta
        # como actividad y, comprobada antes, un reintento del outbox caería al texto y duplicaría el aviso.
        return CONVERSACION_ACTIVA if await _conversacion_activa(conversation.id, tenant_id) else ""

    resultado = await enviar_opciones(
        conversation.id,
        clave,
        datos=datos_del_evento(evento, ctx),
        clave_idempotencia=notice_key,
        actor=f"evento:{evento}"[:120],
        sin_mensaje_de_fallo=True,
        antes_de_enviar=_guarda_de_turno if tipo in ac.TIPOS_DE_TEXTO else None,
    )
    if resultado.outcome == ev.DUPLICADO:
        return NoticeResult("duplicado")
    if resultado.enviado:
        return NoticeResult("opciones", ev.ETIQUETA_VIA.get(resultado.via or "", ""))
    _cae(evento, conversation.id, canal, resultado.outcome, resultado.motivo)
    if resultado.outcome == ev.ERROR_INTERNO:
        # Ambiguo: pudo fallar DESPUÉS de persistir o encolar. Caer al texto podría duplicar el aviso; reintentar el
        # evento es seguro (el `send_id` es determinista y un envío ya hecho vuelve como `duplicate`).
        raise NoticeNotSentError(f"Aviso {evento} con opciones sin confirmar ({resultado.motivo})")
    return None


async def error_de_mapeo(tenant_id: uuid.UUID, evento: str, clave: str) -> str:
    """Por qué `clave` no puede ser la plantilla con opciones del `evento` de este negocio, o "" si puede. Es la
    validación del `PUT /admin/settings/message-templates`: el evento no es de cobranza, la clave tiene la forma de
    `option_templates.key`, existe en el negocio (no borrada), está publicada con una versión vigente no retirada y
    todo dato que su cuerpo y sus acciones necesitan lo trae el evento (o es de la base). Sin lo último el lint la
    aceptaría —las rutas de datos son globales— y el aviso caería en silencio al texto por `contexto_incompleto`."""
    from pydantic import ValidationError
    from sqlalchemy import select

    from core.db.models import OptionTemplate, OptionTemplateVersion
    from core.kernel.opciones.definicion import leer as leer_definicion

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("avisos con opciones: fuera del tenant_scope del negocio")
    if evento == "provisioning.invitation":
        return "El aviso de la invitación del comprador no admite plantilla con opciones: sus datos (negocio, cliente) son del comprador."
    if evento in COBRANZA:
        return "Los avisos de cobranza no admiten plantilla con opciones: no se puede garantizar el horario legal de entrega."
    if not FORMA_DE_CLAVE.fullmatch(clave):
        return "La clave de la plantilla con opciones no es válida (minúsculas, números y _)."
    async with get_session() as session:
        plantilla = (
            await session.execute(
                select(OptionTemplate).where(
                    OptionTemplate.tenant_id == tenant_id, OptionTemplate.key == clave, OptionTemplate.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if plantilla is None:
            return f"No existe la plantilla con opciones «{clave}» en este negocio."
        if plantilla.status != "published" or plantilla.current_version_n is None:
            return f"La plantilla con opciones «{clave}» no está publicada."
        version = (
            await session.execute(
                select(OptionTemplateVersion).where(
                    OptionTemplateVersion.tenant_id == tenant_id,
                    OptionTemplateVersion.option_template_id == plantilla.id,
                    OptionTemplateVersion.version_n == plantilla.current_version_n,
                )
            )
        ).scalar_one_or_none()
    if version is None or version.retired_at is not None:
        return f"La versión vigente de la plantilla con opciones «{clave}» está retirada."
    try:
        definicion = leer_definicion(version.definition)
    except ValidationError:
        return f"La definición de la plantilla con opciones «{clave}» no es válida."
    necesarias, _ = contexto.rutas_citadas(definicion)
    del_evento = {r for r in EVENTOS[evento] if r in contexto.RUTAS_DE_DATOS} | set(contexto.VARIABLES_BASE)
    faltan = [r for r in necesarias if r not in del_evento]
    if faltan:
        return f"La plantilla con opciones «{clave}» usa datos que este evento no tiene: {', '.join(faltan[:5])}."
    return ""
