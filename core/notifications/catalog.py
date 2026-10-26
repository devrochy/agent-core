"""Eventos de notificación que son del core (ADR-0167).

Los textos de los eventos de un pack viven en el pack. Aquí solo están los de core:
escalación, conexión pausada y mensajes fallidos. Son constantes de código (infraestructura
determinista, no prompts) y `data` lleva solo ids, nunca datos del cliente.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from core.notifications.spec import NotificationSpec

TITULO_ESCALACION = "Nueva escalación"
TITULO_CONEXION_WHATSAPP = "WhatsApp desactivado"
TITULO_CONEXION_PROPIA = "Conexión pausada"


def escalacion(conversation_id: uuid.UUID | str, reason: str, escalation_id: uuid.UUID | str) -> NotificationSpec:
    """Un cliente pidió atención humana y la conversación quedó escalada."""
    return NotificationSpec(
        event_key="core.escalation",
        category="conversaciones",
        severity="critical",
        title=TITULO_ESCALACION,
        body="Un cliente pidió atención humana y la conversación quedó escalada.",
        dedupe_key=f"escalacion:{escalation_id}",
        section="escalations",
        # `type`/`conversationId`/`reason`: lo que ya lee la app instalada (deep link legacy).
        data={"type": "core.escalation", "conversationId": str(conversation_id), "reason": reason},
        target={"entity": "conversacion", "id": str(conversation_id), "section": "escalations"},
    )


def escalacion_fallida(conversation_id: uuid.UUID | str) -> NotificationSpec:
    """El cliente pidió una persona y el sistema no pudo escalar: nadie más se enteraría."""
    return NotificationSpec(
        event_key="core.escalation_failed",
        category="conversaciones",
        severity="critical",
        title=TITULO_ESCALACION,
        body="Un cliente pidió atención humana y no se pudo escalar la conversación. Atiéndela desde el panel.",
        dedupe_key=f"escalacion_fallida:{conversation_id}",
        section="escalations",
        data={"type": "core.escalation", "conversationId": str(conversation_id), "reason": "escalation_failed"},
        target={"entity": "conversacion", "id": str(conversation_id), "section": "escalations"},
    )


def conexion_pausada(
    connection_id: uuid.UUID | str, *, alcance: str, motivo: str, dia: str
) -> NotificationSpec:
    """Llegan mensajes de clientes y el asistente no los responde porque la conexión está pausada.

    `dia` (AAAA-MM-DD) acota el episodio: un aviso por conexión y día, como el push de antes."""
    return NotificationSpec(
        event_key="core.connection_paused",
        category="sistema",
        severity="critical",
        title=TITULO_CONEXION_WHATSAPP if alcance == "meta_account" else TITULO_CONEXION_PROPIA,
        body=(
            f"Llegaron mensajes de clientes y el asistente no los responde: {motivo.lower()}. "
            "Reactívala en Conexiones."
        ),
        dedupe_key=f"conexion_pausada:{connection_id}:{dia}",
        section="connections",
        data={"type": "core.connection_paused", "connectionId": str(connection_id), "scope": alcance},
        target={"entity": "conexion", "id": str(connection_id), "section": "connections"},
    )


def conexion_sin_acceso(connection_id: uuid.UUID | str, *, vencido: bool, dia: str) -> NotificationSpec:
    """El token de Meta de la conexión venció o Meta lo marca inválido: hay que reautorizar."""
    return NotificationSpec(
        event_key="core.connection_token_invalid",
        category="sistema",
        severity="critical",
        title="Conexión de WhatsApp sin acceso",
        body=(
            "El acceso de tu conexión con Meta venció. Reconéctala en Conexiones para no dejar de responder."
            if vencido
            else "Meta reporta el acceso de tu conexión como inválido. Reautorízala en Conexiones."
        ),
        dedupe_key=f"conexion_token_invalido:{connection_id}:{dia}",
        section="connections",
        data={"type": "core.connection_token", "connectionId": str(connection_id)},
        target={"entity": "conexion", "id": str(connection_id), "section": "connections"},
    )


def conexion_token_por_vencer(connection_id: uuid.UUID | str, *, vence: str, dia: str) -> NotificationSpec:
    return NotificationSpec(
        event_key="core.connection_token_expiring",
        category="sistema",
        severity="warning",
        title="El acceso de tu conexión vence pronto",
        body=f"El acceso de tu conexión con Meta vence el {vence}. Reconéctala en Conexiones antes de esa fecha.",
        dedupe_key=f"conexion_token_por_vencer:{connection_id}:{dia}",
        section="connections",
        data={"type": "core.connection_token", "connectionId": str(connection_id)},
        target={"entity": "conexion", "id": str(connection_id), "section": "connections"},
    )


def calidad_de_numero_roja(connection_id: uuid.UUID | str, *, dia: str) -> NotificationSpec:
    return NotificationSpec(
        event_key="core.number_quality_red",
        category="sistema",
        severity="warning",
        title="Calidad de tu número en rojo",
        body="Meta calificó tu número de WhatsApp en rojo: hay riesgo de bloqueo. Revisa los mensajes que envías.",
        dedupe_key=f"calidad_roja:{connection_id}:{dia}",
        section="connections",
        data={"type": "core.number_quality", "connectionId": str(connection_id)},
        target={"entity": "conexion", "id": str(connection_id), "section": "connections"},
    )


def mensaje_fallido(entry_id: str) -> NotificationSpec:
    """Un mensaje entrante agotó los reintentos y quedó en la bandeja de fallos."""
    return NotificationSpec(
        event_key="core.message_dead_lettered",
        category="sistema",
        severity="critical",
        title="Un mensaje no se pudo procesar",
        body="Un mensaje de un cliente agotó los reintentos. Revísalo en Mensajes fallidos.",
        dedupe_key=f"mensaje_fallido:{entry_id}",
        section="settings",
        data={"type": "core.dlq"},
        target={"entity": "mensaje_fallido", "id": entry_id, "section": "settings"},
    )


def handoff_tomado(
    escalation_id: uuid.UUID | str,
    conversation_id: uuid.UUID | str,
    admin_id: uuid.UUID | str,
    quien: str,
) -> NotificationSpec:
    """Una persona del equipo tomó la conversación escalada: el resto sabe que ya la atienden.

    `actorAdminId` (en `data`) es quien hizo la acción: no recibe el push ni cuenta como no leída."""
    return NotificationSpec(
        event_key="core.handoff_assigned",
        category="conversaciones",
        severity="info",
        title="Conversación tomada",
        body=f"{quien} atiende la conversación escalada.",
        dedupe_key=f"handoff_tomado:{escalation_id}:{admin_id}",
        section="escalations",
        data={
            "type": "core.handoff_assigned",
            "conversationId": str(conversation_id),
            "escalationId": str(escalation_id),
            "actorAdminId": str(admin_id),
        },
        target={"entity": "conversacion", "id": str(conversation_id), "section": "escalations"},
    )


def handoff_resuelto(
    escalation_id: uuid.UUID | str,
    conversation_id: uuid.UUID | str,
    admin_id: uuid.UUID | str,
    quien: str,
    *,
    devuelta_al_agente: bool,
) -> NotificationSpec:
    """La atención humana de la conversación terminó (resuelta o devuelta al agente)."""
    return NotificationSpec(
        event_key="core.handoff_resolved",
        category="conversaciones",
        severity="success",
        title="Conversación resuelta",
        body=(
            f"{quien} devolvió la conversación al agente."
            if devuelta_al_agente
            else f"{quien} cerró la atención humana de la conversación."
        ),
        dedupe_key=f"handoff_resuelto:{escalation_id}",
        section="escalations",
        data={
            "type": "core.handoff_resolved",
            "conversationId": str(conversation_id),
            "escalationId": str(escalation_id),
            "actorAdminId": str(admin_id),
        },
        target={"entity": "conversacion", "id": str(conversation_id), "section": "escalations"},
    )


def cliente_esperando(
    escalation_id: uuid.UUID | str,
    conversation_id: uuid.UUID | str,
    *,
    desde: datetime,
    umbral_minutos: int,
    asignada_a: uuid.UUID | None = None,
) -> NotificationSpec:
    """Un cliente lleva más de `umbral_minutos` sin respuesta humana en una conversación escalada.

    `desde` (inicio del episodio) acota la deduplicación: un aviso por episodio. Si la
    escalación está asignada, va dirigido a esa persona además de a quien ve Escalaciones."""
    return NotificationSpec(
        event_key="core.customer_waiting",
        category="conversaciones",
        severity="warning",
        title="Cliente esperando respuesta",
        body=f"Un cliente lleva más de {umbral_minutos} minutos esperando respuesta humana.",
        dedupe_key=f"cliente_esperando:{escalation_id}:{int(desde.timestamp())}",
        section="escalations",
        audience_admin_id=asignada_a,
        data={
            "type": "core.customer_waiting",
            "conversationId": str(conversation_id),
            "escalationId": str(escalation_id),
        },
        target={"entity": "conversacion", "id": str(conversation_id), "section": "escalations"},
    )
