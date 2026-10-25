"""Avisos por evento con plantillas de WhatsApp (HU-ARCH-13, ADR-0108).

Un aviso (cobro, recordatorio, seguimiento) sale así:

- ventana de 24 h abierta → texto de respaldo (más barato que una plantilla);
- ventana cerrada y el evento mapeado a una plantilla APROBADA de la WABA de la
  conversación → la plantilla, con las variables resueltas del contexto del evento;
- ventana cerrada sin mapeo (o la plantilla no está aprobada) → se registra un
  mensaje `failed` con `error_reason = "sin_plantilla"` y el panel lo muestra.

Las variables salen SOLO del contexto que arma el servidor a partir del evento y de
la base (rutas permitidas por evento en `EVENTOS`). Nunca del texto del LLM.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from core.db.models import AppSettings, Conversation, Message, MessageTemplate, WabaAccount
from core.db.session import get_session
from core.shared.exceptions import OutboundWindowExpiredError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

SIN_PLANTILLA = "sin_plantilla"

# Evento → rutas del contexto que se pueden usar como variables. Lo que no está
# aquí no se puede mapear (el panel solo ofrece estas rutas).
_VARIABLES_CARGO = {
    "cliente.nombre": "Nombre del cliente",
    "suscripcion.concepto": "Plan o concepto de la suscripción",
    "cargo.monto": "Monto del cargo",
    "cargo.moneda": "Moneda",
    "cargo.periodo": "Período (AAAA-MM)",
    "cargo.fecha_vencimiento": "Fecha de vencimiento",
    "cargo.referencia": "Referencia del pago",
    "cargo.link": "Link de pago",
    "cargo.datos_transferencia": "Datos de transferencia del negocio (cuentas)",
    "cargo.forma_de_pago": "Cómo pagar: el link o, si no hay, los datos de transferencia",
}

_VARIABLES_CITA = {
    "cliente.nombre": "Nombre del cliente",
    "cita.titulo": "Tipo o título de la cita",
    "cita.fecha": "Fecha de la cita",
    "cita.hora": "Hora de la cita",
    "cita.zona": "Zona horaria",
    "cita.enlace": "Enlace de la reunión",
}

EVENTOS: dict[str, dict[str, str]] = {
    "subscription.charge_created": dict(_VARIABLES_CARGO),
    # HU-SUB-05: recordatorios y pausa por mora.
    "subscription.charge_reminder": {
        **_VARIABLES_CARGO,
        "recordatorio.dias": "Cuándo vence (\"en 3 días\", \"hoy\", \"hace 3 días\")",
    },
    "subscription.paused": {
        "cliente.nombre": "Nombre del cliente",
        "suscripcion.concepto": "Plan o concepto de la suscripción",
        "cargo.monto": "Monto adeudado",
        "cargo.periodo": "Período adeudado",
        "cargo.link": "Link de pago",
        "cargo.datos_transferencia": "Datos de transferencia del negocio (cuentas)",
        "cargo.forma_de_pago": "Cómo pagar: el link o, si no hay, los datos de transferencia",
        "mora.dias": "Días de mora",
    },
    # HU-SH-02: citas (al cliente).
    "appointment.confirmed": dict(_VARIABLES_CITA),
    "appointment.reminder": dict(_VARIABLES_CITA),
    "appointment.no_show": dict(_VARIABLES_CITA),
    # HU-SUB-06: oferta de un plan pago antes de que termine la prueba de Free.
    "subscription.trial_ending": {
        "cliente.nombre": "Nombre del cliente",
        "suscripcion.concepto": "Plan o concepto de la suscripción",
        "prueba.termina": "Fecha en que termina la prueba",
    },
    # HU-SH-03 (ADR-0121): el acceso al negocio comprado llega por correo. Sin enlace ni
    # token: el enlace va SOLO por correo y el aviso es igual en todos los casos.
    "provisioning.invitation": {
        "cliente.nombre": "Nombre del cliente",
        "negocio.nombre": "Nombre del negocio",
        "invitacion.correo": "Correo al que llega el acceso (enmascarado)",
    },
}

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


class NoticeConfigError(ValueError):
    """La configuración de un evento no es válida (ruta, plantilla o variables)."""


class NoticeNotSentError(RuntimeError):
    """El aviso no se pudo encolar (pausa por uso, carrera con la ventana): se
    lanza para que el outbox reintente el evento con backoff (ADR-0107)."""


MAX_PARAM = 1024


def clean_param(valor: str, limite: int = MAX_PARAM) -> str:
    """Parámetro apto para Meta: sin saltos ni tabulaciones, sin más de un espacio
    seguido y con largo acotado (Meta rechaza el parámetro, 132000/132018). Se
    aplica a todo valor, venga del evento o de la persona del equipo."""
    texto = re.sub(r"[\r\n\t]+", " ", str(valor))
    texto = re.sub(r" {2,}", " ", texto).strip()
    return texto[:limite].rstrip()


@dataclass(frozen=True)
class NoticeResult:
    modo: str  # "texto" | "plantilla" | "sin_plantilla" | "duplicado" | "no_enviado" | "conexion_pausada"
    detalle: str = ""


# ---------------------------------------------------------------- plantillas
def _component(template_components: Any, tipo: str) -> dict[str, Any] | None:
    for comp in template_components or []:
        if isinstance(comp, dict) and str(comp.get("type", "")).upper() == tipo:
            return comp
    return None


def template_placeholders(template_components: Any) -> list[str]:
    """Placeholders del cuerpo en orden de aparición, sin repetir ("1", "2" o nombres)."""
    body = _component(template_components, "BODY")
    vistos: list[str] = []
    for nombre in _PLACEHOLDER.findall(str((body or {}).get("text", ""))):
        if nombre not in vistos:
            vistos.append(nombre)
    return vistos


def template_has_non_body_variables(template_components: Any) -> bool:
    """v1 solo resuelve variables del cuerpo: cabecera o botones con variables no se mapean."""
    for comp in template_components or []:
        if not isinstance(comp, dict) or str(comp.get("type", "")).upper() == "BODY":
            continue
        if _PLACEHOLDER.search(str(comp.get("text", ""))):
            return True
        for boton in comp.get("buttons") or []:
            if isinstance(boton, dict) and _PLACEHOLDER.search(str(boton.get("url", ""))):
                return True
    return False


def build_components(template: MessageTemplate, values: dict[str, str]) -> list[dict[str, Any]]:
    """Componentes de Graph para el cuerpo. `values`: placeholder → texto."""
    placeholders = template_placeholders(template.components)
    if not placeholders:
        return []
    named = (template.parameter_format or "").upper() == "NAMED"
    parametros = []
    for nombre in placeholders:
        parametro: dict[str, Any] = {"type": "text", "text": clean_param(values[nombre])}
        if named:
            parametro["parameter_name"] = nombre
        parametros.append(parametro)
    return [{"type": "body", "parameters": parametros}]


def render_body(template: MessageTemplate, values: dict[str, str]) -> str:
    body = _component(template.components, "BODY")
    texto = str((body or {}).get("text", ""))
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), texto)


# ---------------------------------------------------------------- variables
def _lookup(context: dict[str, Any], ruta: str) -> str | None:
    actual: Any = context
    for parte in ruta.split("."):
        if not isinstance(actual, dict) or parte not in actual:
            return None
        actual = actual[parte]
    if actual is None or isinstance(actual, dict | list):
        return None
    texto = clean_param(str(actual))
    return texto or None


def resolve_values(
    event_key: str, variables: list[str] | dict[str, str], placeholders: list[str], context: dict[str, Any]
) -> dict[str, str]:
    """placeholder → valor, solo con rutas permitidas del evento. Lanza
    `NoticeConfigError` si una ruta no está permitida o falta el dato."""
    permitidas = EVENTOS.get(event_key, {})
    if isinstance(variables, dict):
        mapeo = {str(k): str(v) for k, v in variables.items()}
    else:
        mapeo = {nombre: str(ruta) for nombre, ruta in zip(placeholders, variables, strict=False)}
    if set(mapeo) != set(placeholders):
        raise NoticeConfigError(
            f"La plantilla pide {len(placeholders)} variables y el mapeo tiene {len(mapeo)}."
        )
    valores: dict[str, str] = {}
    for nombre in placeholders:
        ruta = mapeo[nombre]
        if ruta not in permitidas:
            raise NoticeConfigError(f"La ruta «{ruta}» no está permitida para {event_key}.")
        valor = _lookup(context, ruta)
        if valor is None:
            raise NoticeConfigError(f"Falta el dato «{ruta}» en el evento.")
        valores[nombre] = valor
    return valores


def render_fallback(texto: str, context: dict[str, Any], event_key: str) -> str:
    """Texto de respaldo con `{ruta}` reemplazadas por datos del evento (solo rutas permitidas)."""
    permitidas = EVENTOS.get(event_key, {})

    def _sub(m: re.Match[str]) -> str:
        ruta = m.group(1)
        if ruta not in permitidas:
            return m.group(0)
        return _lookup(context, ruta) or ""

    return re.sub(r"\{([a-z_]+\.[a-z_]+)\}", _sub, texto)


# ---------------------------------------------------------------- config
async def load_templates_map(tenant_id: uuid.UUID) -> dict[str, Any]:
    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings).where(
                    AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
    return dict((app_settings.message_templates_map if app_settings else None) or {})


async def approved_template_for(
    conversation: Conversation, name: str, language: str
) -> MessageTemplate | None:
    """Plantilla APROBADA con ese nombre e idioma en la WABA de la conexión de la
    conversación. Sin conexión de Meta no hay plantillas. La cuenta es la misma con la que
    salen las credenciales (ADR-0115 §2): la de la conexión canónica si la hay."""
    from core.gateway.meta_connection_state import cuenta_de_plantillas_de_conversacion

    cuenta = await cuenta_de_plantillas_de_conversacion(conversation)
    if cuenta is None:
        return None
    async with get_session() as session:
        return (
            await session.execute(
                select(MessageTemplate)
                .join(WabaAccount, WabaAccount.id == MessageTemplate.waba_account_id)
                .where(
                    WabaAccount.connection_id == cuenta,
                    WabaAccount.deleted_at.is_(None),
                    WabaAccount.tenant_id == conversation.tenant_id,
                    MessageTemplate.tenant_id == conversation.tenant_id,
                    MessageTemplate.name == name,
                    MessageTemplate.language == language,
                    MessageTemplate.status == "APPROVED",
                )
            )
        ).scalars().first()


async def approved_templates_for_conversation(conversation: Conversation) -> list[MessageTemplate]:
    from core.gateway.meta_connection_state import cuenta_de_plantillas_de_conversacion

    cuenta = await cuenta_de_plantillas_de_conversacion(conversation)
    if cuenta is None:
        return []
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(MessageTemplate)
                    .join(WabaAccount, WabaAccount.id == MessageTemplate.waba_account_id)
                    .where(
                        WabaAccount.connection_id == cuenta,
                        WabaAccount.deleted_at.is_(None),
                        WabaAccount.tenant_id == conversation.tenant_id,
                        MessageTemplate.tenant_id == conversation.tenant_id,
                        MessageTemplate.status == "APPROVED",
                    )
                    .order_by(MessageTemplate.name, MessageTemplate.language)
                )
            ).scalars().all()
        )


# ---------------------------------------------------------------- envío
async def _ya_enviado(notice_key: str) -> bool:
    async with get_session() as session:
        existe = (
            await session.execute(
                select(Message.id).where(
                    Message.tenant_id == get_current_tenant_id(),
                    Message.client_message_id.in_((notice_key, f"{notice_key}:0")),
                )
            )
        ).first()
    return existe is not None


async def _registrar_sin_plantilla(
    conversation_id: uuid.UUID, notice_key: str, event_key: str, detalle: str
) -> None:
    async with get_session() as session:
        session.add(
            Message(
                tenant_id=get_current_tenant_id(),
                conversation_id=conversation_id,
                direction="outbound",
                sender_type="agent",
                content=f"Aviso «{event_key}» sin enviar: {detalle}",
                status="failed",
                error_reason=SIN_PLANTILLA,
                client_message_id=notice_key,
            )
        )
    log.warning(
        "event_notice_sin_plantilla",
        tenant_id=str(get_current_tenant_id()),
        conversation_id=str(conversation_id),
        event_key=event_key,
        detalle=detalle,
    )


async def send_event_notice(
    conversation_id: uuid.UUID,
    event_key: str,
    context: dict[str, Any],
    *,
    notice_key: str,
    default_fallback_text: str = "",
) -> NoticeResult:
    """Envía el aviso de `event_key` a la conversación (ver el docstring del módulo).

    `notice_key` es estable por aviso (p. ej. `sub_charge:<cargo>`): un reintento
    del evento no duplica el mensaje. Corre dentro del `tenant_scope` del evento.
    """
    from core.db.models import utcnow
    from core.gateway.sender import send_template_to_conversation, send_to_conversation

    if await _ya_enviado(notice_key):
        return NoticeResult("duplicado")
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.tenant_id != get_current_tenant_id():
        return NoticeResult("no_enviado", "conversación inexistente")

    # Conexión pausada o eliminada (ADR-0122, enmienda a ADR-0108): el aviso no se
    # reintenta. La pausa es a propósito y puede durar días; con `NoticeNotSentError` el
    # outbox lo reintentaría 5 veces, dejando 5 mensajes fallidos, y lo perdería igual.
    # Queda un solo `failed` con el motivo, con el id estable del aviso.
    from core.gateway.meta_connection_state import motivo_pausa_de_conversacion

    if motivo := await motivo_pausa_de_conversacion(conversation):
        async with get_session() as session:
            session.add(
                Message(
                    tenant_id=get_current_tenant_id(),
                    conversation_id=conversation_id,
                    direction="outbound",
                    sender_type="agent",
                    content=f"Aviso «{event_key}» sin enviar: {motivo}",
                    status="failed",
                    error_reason=motivo,
                    client_message_id=notice_key,
                )
            )
        log.info("event_notice_conexion_pausada", conversation_id=str(conversation_id), event_key=event_key)
        return NoticeResult("conexion_pausada", motivo)

    entrada = (await load_templates_map(conversation.tenant_id)).get(event_key) or {}
    fallback = str(entrada.get("fallback_text") or default_fallback_text).strip()

    # Ventana desconocida (sin `service_window_expires_at`: filas viejas o una
    # conversación que no abrió un mensaje entrante) cuenta como CERRADA: un texto
    # libre ahí lo rechaza Meta (131047) sin dejar rastro de `sin_plantilla`.
    expira = conversation.service_window_expires_at
    ventana_abierta = expira is not None and expira >= utcnow()

    if ventana_abierta:
        if not fallback:
            return NoticeResult("no_enviado", "sin texto de respaldo")
        try:
            enviado = await send_to_conversation(
                conversation_id,
                render_fallback(fallback, context, event_key),
                client_message_id_prefix=notice_key,
            )
        except OutboundWindowExpiredError:
            enviado = False
        if not enviado:
            raise NoticeNotSentError(f"Aviso {event_key} sin encolar (pausa o ventana cerrada)")
        return NoticeResult("texto")

    nombre = str(entrada.get("template") or "").strip()
    idioma = str(entrada.get("language") or "es").strip()
    if not nombre:
        await _registrar_sin_plantilla(conversation_id, notice_key, event_key, "evento sin plantilla")
        return NoticeResult(SIN_PLANTILLA, "evento sin plantilla")
    plantilla = await approved_template_for(conversation, nombre, idioma)
    if plantilla is None:
        detalle = f"la plantilla {nombre} ({idioma}) no está aprobada en esta conexión"
        await _registrar_sin_plantilla(conversation_id, notice_key, event_key, detalle)
        return NoticeResult(SIN_PLANTILLA, detalle)
    try:
        valores = resolve_values(
            event_key,
            entrada.get("variables") or [],
            template_placeholders(plantilla.components),
            context,
        )
    except NoticeConfigError as exc:
        await _registrar_sin_plantilla(conversation_id, notice_key, event_key, str(exc))
        return NoticeResult(SIN_PLANTILLA, str(exc))

    enviado = await send_template_to_conversation(
        conversation_id,
        template_name=plantilla.name,
        language=plantilla.language,
        components=build_components(plantilla, valores),
        preview_text=render_body(plantilla, valores),
        client_message_id=notice_key,
    )
    if not enviado:
        raise NoticeNotSentError(f"Aviso {event_key} sin encolar (pausa por uso de la API)")
    return NoticeResult("plantilla")
