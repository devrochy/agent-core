"""Avisos por evento por canal de texto (ADR-0178 D2, extiende ADR-0108): la decisión de vía y sus guardas.

Hasta D2 un aviso por evento solo salía por conversaciones de `whatsapp`, y como plantilla de Meta o texto según la
ventana de 24 h. En QR (waweb), chat web, Messenger o Instagram el aviso no se enviaba. Con la bandera del negocio
encendida (`avisos_bandera`), `send_event_notice` decide la vía con la MISMA tabla de capacidades que las opciones
(`connection_types.capacidades_de`, pista C1):

- el canal admite plantilla aprobada (WhatsApp Meta) → lo de siempre (plantilla o texto según la ventana);
- el canal no tiene plantillas (QR, web, Messenger, Instagram) y el negocio definió el texto de ese canal en
  `message_templates_map[<evento>]["fallback_text_by_channel"][<tipo de conexión>]` → ese texto;
- sin texto definido no se envía nada nuevo: se registra `event_notice_omitido_por_canal` y la métrica
  `event_notice_omitted_total`.

Las claves de `fallback_text_by_channel` son `ConnectionType.id` (no el canal: WhatsApp Meta y QR son el mismo canal).
Este módulo es el único que sabe qué hacer con ellas; `send_event_notice` solo pregunta.

**Políticas de cada canal, que este camino no salta:**

- *QR (ADR-0074/0103).* El aviso es un envío no solicitado, y el modo por defecto del canal es «solo respuesta»:
  solo sale si el negocio activó `waweb_mode = allow_outbound`. Además se mira el tope diario y la pausa ANTES de
  encolar (`limits.allow_send`): el conector difiere un mensaje bloqueado hasta ~24 h (00:02 UTC), y un aviso diferido
  así podría llegar fuera del horario legal de cobranza. Los eventos de cobranza (`COBRANZA`) NO salen por QR:
  aun con esas guardas queda una carrera (conector caído, pausa a mano) que no permite garantizar el horario de
  HU-SUB-18 en la entrega. El delay humano y el contador del día los sigue poniendo el conector.
- *Messenger e Instagram.* El asistente nunca escribe fuera de la ventana de 24 h (`sender.validate_service_window`,
  ADR-0152): ventana desconocida o vencida = no se envía (como ADR-0108 para WhatsApp).
- *Chat web.* Sin proveedor externo, ventana ni plantillas: el mensaje se guarda y el widget lo recoge.
- *Horario legal de cobranza (HU-SUB-18).* Quien decide CUÁNDO se emite el aviso sigue siendo quien lo emite
  (`pack_subscriptions`: cron, diferido del cargo nuevo y reenvío del panel); este módulo no cambia esa hora y
  solo permite la entrega inmediata (web, Messenger, Instagram).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from opentelemetry import metrics
from sqlalchemy import select

from core.db.models import ChannelConnection, Conversation
from core.db.session import get_session
from core.gateway.connection_types import capacidades_de, tipo_de_conexion
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Tipos de conexión que pueden recibir un aviso como texto de respaldo (claves válidas de
# `fallback_text_by_channel`). WhatsApp por Twilio/Plivo, correo y el playground quedan fuera a propósito.
QR = "whatsapp_waweb"
WEB = "webchat"
MESSENGER = "messenger_meta"
INSTAGRAM = "instagram_meta"
TIPOS_DE_TEXTO: tuple[str, ...] = (QR, WEB, MESSENGER, INSTAGRAM)
# Canales de Meta con ventana de 24 h para el asistente aunque no tengan plantillas (la tabla de C1 no lo dice:
# `ventana_24h` allí significa «ventana + plantilla»; `sender.validate_service_window` es lo que rige).
CON_VENTANA = frozenset({MESSENGER, INSTAGRAM})
# Eventos de cobranza (Ley 2300, HU-SUB-18): sin garantía de horario en la entrega por QR (ver el docstring).
COBRANZA = frozenset({"subscription.charge_created", "subscription.charge_reminder", "subscription.paused"})

# Vías de la decisión.
LEGACY = "legacy"  # lo de siempre (WhatsApp Meta, o un canal sin texto definido que ya tenía su camino)
TEXTO = "texto"
OMITIDO = "omitido"

# Motivos (conjunto cerrado: van a una métrica y al log; nunca datos del cliente).
SIN_RESPALDO = "sin_texto_de_respaldo"
VENTANA_CERRADA = "ventana_cerrada"
COBRANZA_SIN_HORARIO = "cobranza_sin_garantia_de_horario"
SOLO_RESPUESTA = "waweb_solo_respuesta"
TOPE_O_PAUSA = "waweb_tope_o_pausa"
TEXTO_VACIO = "texto_vacio"
VARIABLES_SIN_RESOLVER = "variables_sin_resolver"

_meter = metrics.get_meter("agent-core")
_omitidos = _meter.create_counter(
    "event_notice_omitted_total", description="Avisos por evento que no salieron por un canal de texto, por motivo"
)


@dataclass(frozen=True)
class DecisionAviso:
    """`via` LEGACY = sigue el camino de siempre; `motivo` (si lo hay) se registra aunque la vía sea LEGACY."""

    via: str
    tipo: str | None = None
    motivo: str = ""


def decidir_via_aviso(
    *, canal: str, proveedor: str, evento: str, texto_definido: bool, ventana_abierta: bool
) -> DecisionAviso:
    """La vía de un aviso con la bandera encendida. Pura: lo que cada canal admite sale de `capacidades_de`.

    `ventana_abierta`: ventana de 24 h conocida y vigente (desconocida = cerrada, ADR-0108). Solo importa en los
    canales de `CON_VENTANA`."""
    tipo = tipo_de_conexion(canal, proveedor)
    if tipo not in TIPOS_DE_TEXTO or capacidades_de(canal, proveedor).plantilla_aprobada:
        return DecisionAviso(LEGACY, tipo)
    if not texto_definido:
        # QR ya tenía su camino (hoy deja un `sin_plantilla` visible en el panel): se conserva. El resto no tenía
        # ni conversación que encontrar, así que no hay nada que conservar.
        return DecisionAviso(LEGACY if canal == "whatsapp" else OMITIDO, tipo, SIN_RESPALDO)
    if tipo == QR and evento in COBRANZA:
        return DecisionAviso(LEGACY, tipo, COBRANZA_SIN_HORARIO)
    if tipo in CON_VENTANA and not ventana_abierta:
        return DecisionAviso(OMITIDO, tipo, VENTANA_CERRADA)
    return DecisionAviso(TEXTO, tipo)


def texto_del_canal(entrada: dict[str, Any], tipo: str | None) -> str:
    """Texto definido por el negocio para ese tipo de conexión, o "" (también si el valor no es un texto)."""
    textos = entrada.get("fallback_text_by_channel")
    if tipo is None or not isinstance(textos, dict):
        return ""
    valor = textos.get(tipo)
    return valor.strip() if isinstance(valor, str) else ""


def aviso_omitido(evento: str, canal: str, motivo: str, *, conversation_id: uuid.UUID | None = None) -> None:
    """Deja rastro de un aviso que no salió por un canal de texto: log estructurado y métrica. Antes de D2 esto era
    silencio. Etiquetas: conjuntos cerrados (evento de `EVENTOS`, canal, motivo)."""
    _omitidos.add(1, {"evento": evento[:60], "canal": canal[:20], "motivo": motivo})
    log.warning(
        "event_notice_omitido_por_canal",
        tenant_id=str(get_current_tenant_id()),
        conversation_id=str(conversation_id) if conversation_id else None,
        evento=evento,
        canal=canal,
        motivo=motivo,
    )


async def proveedor_de_conversacion(conversation: Conversation) -> str:
    """Proveedor de una conversación de WhatsApp (`meta`, `waweb`, `twilio`…), o "" si no se sabe. Una consulta a la
    conexión (no descifra credenciales). Los demás canales tienen un solo tipo y no lo necesitan."""
    if conversation.channel != "whatsapp":
        return ""
    if conversation.connection_id is None:
        return "meta" if conversation.meta_connection_id is not None else ""
    async with get_session() as session:
        conexion = await session.get(ChannelConnection, conversation.connection_id)
    if conexion is None or conexion.tenant_id != conversation.tenant_id:
        return ""
    return "meta" if conexion.meta_connection_id is not None else (conexion.provider or "")


async def waweb_bloqueo(tenant_id: uuid.UUID) -> str:
    """Motivo por el que la política anti-baneo del negocio impide un aviso por QR ahora, o "" si lo permite.
    «Solo respuesta» (el modo por defecto) bloquea: un aviso no es una respuesta. Con el envío no solicitado
    habilitado se respeta el tope diario y la pausa: sin cupo no se encola, porque el conector lo difiriría ~24 h."""
    from core.waweb import limits
    from core.waweb.policy import load_policy

    politica = await load_policy(tenant_id)
    if not politica.allows_outbound:
        return SOLO_RESPUESTA
    if not await limits.allow_send(tenant_id, politica):
        return TOPE_O_PAUSA
    return ""


async def conversacion_de_texto_para(customer_id: uuid.UUID, evento: str) -> Conversation | None:
    """Conversación del cliente en un canal de texto sin WhatsApp (web, Messenger, Instagram) para avisarle `evento`.
    Solo se llama cuando el cliente no tiene conversación de WhatsApp (lo de siempre va primero) y la bandera está
    encendida. Entre varias, la más reciente que tenga un texto definido para este evento; si ninguna lo tiene, la
    más reciente (así el aviso omitido queda registrado). Dentro del `tenant_scope` del negocio."""
    from core.gateway.event_notices import load_templates_map

    tenant_id = get_current_tenant_id()
    async with get_session() as session:
        filas = list(
            (
                await session.execute(
                    select(Conversation)
                    .where(
                        Conversation.tenant_id == tenant_id,
                        Conversation.customer_id == customer_id,
                        Conversation.channel.in_(("web", "messenger", "instagram")),
                        Conversation.deleted_at.is_(None),
                    )
                    .order_by(Conversation.started_at.desc())
                    .limit(20)
                )
            ).scalars()
        )
    if not filas:
        return None
    entrada = (await load_templates_map(tenant_id)).get(evento) or {}
    for fila in filas:
        if texto_del_canal(entrada, tipo_de_conexion(fila.channel, "")):
            return fila
    return filas[0]
