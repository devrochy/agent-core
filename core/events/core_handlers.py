"""Handlers del core para eventos de dominio del outbox (ADR-0107, ADR-0108).

Los eventos de dominio solo los entrega el despachador del worker, así que estos
handlers se registran ahí (`ensure_core_handlers`, una vez por proceso).
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from core.billing.canales import on_platform_period_paid, on_subscription_charge_paid
from core.billing.renovacion import on_platform_renewal_due
from core.db.models import Conversation, Customer
from core.db.session import get_session
from core.provisioning.consumer import on_alta_aprobada, on_subscription_activated
from core.shared.events import (
    ON_ALTA_APROBADA,
    ON_APPOINTMENT_CONFIRMED,
    ON_APPOINTMENT_NO_SHOW,
    ON_APPOINTMENT_REMINDER,
    ON_PLATFORM_PERIOD_PAID,
    ON_PLATFORM_RENEWAL_DUE,
    ON_SEGMENT_SEND_BATCH,
    ON_SUBSCRIPTION_ACTIVATED,
    ON_SUBSCRIPTION_CHARGE_CREATED,
    ON_SUBSCRIPTION_CHARGE_PAID,
    ON_SUBSCRIPTION_CHARGE_REMINDER,
    ON_SUBSCRIPTION_PAUSED,
    ON_SUBSCRIPTION_TRIAL_ENDING,
    event_bus,
)
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

_registered_pid: int | None = None


def format_amount(monto: Any, moneda: str | None) -> str:
    """`200000.00` + COP → `$200.000`; otras monedas con dos decimales y su código."""
    try:
        valor = Decimal(str(monto))
    except (InvalidOperation, ValueError):
        return str(monto or "")
    if (moneda or "COP").upper() == "COP":
        return "$" + f"{int(valor.quantize(Decimal('1'))):,}".replace(",", ".")
    return f"{valor:,.2f} {(moneda or '').upper()}".strip()


async def _whatsapp_conversation_for(customer_id: uuid.UUID) -> Conversation | None:
    """Conversación de WhatsApp más reciente del cliente (la de su número)."""
    async with get_session() as session:
        return (
            await session.execute(
                select(Conversation)
                .where(
                    Conversation.tenant_id == get_current_tenant_id(),
                    Conversation.customer_id == customer_id,
                    Conversation.channel == "whatsapp",
                    Conversation.deleted_at.is_(None),
                )
                .order_by(Conversation.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


def notice_key_for(topic: str, payload: dict[str, Any]) -> str:
    """Clave estable del aviso: un reintento del evento no lo duplica (ADR-0108)."""
    cargo = payload.get("charge_id")
    if topic == ON_SUBSCRIPTION_CHARGE_REMINDER:
        return f"sub_reminder:{cargo}:{payload.get('dias')}"
    if topic == ON_SUBSCRIPTION_PAUSED:
        return f"sub_paused:{payload.get('subscription_id')}:{cargo}"
    if topic == ON_SUBSCRIPTION_TRIAL_ENDING:
        return f"sub_trial_ending:{payload.get('subscription_id')}"
    if topic == ON_APPOINTMENT_REMINDER:
        return f"appt_reminder:{payload.get('appointment_id')}:{payload.get('horas')}"
    if topic in (ON_APPOINTMENT_NO_SHOW, ON_APPOINTMENT_CONFIRMED):
        return f"{topic}:{payload.get('appointment_id')}"
    return f"sub_charge:{cargo}"


async def _avisar_suscripcion(topic: str, payload: dict[str, Any]) -> None:
    """Aviso al cliente de un evento de suscripción o de cita (plantilla o texto según la ventana).

    El texto de respaldo lo manda el pack en `payload["texto"]` (su config, editable
    por el negocio); las variables salen solo del contexto que arma este handler.
    """
    from core.gateway.event_notices import clean_param, send_event_notice

    try:
        customer_id = uuid.UUID(str(payload.get("customer_id")))
    except ValueError:
        log.warning("event_notice_payload_invalido", topic=topic)
        return
    conversation = await _whatsapp_conversation_for(customer_id)
    if conversation is None:
        # Sin conversación de WhatsApp no hay a quién escribir: no es un fallo
        # que valga reintentar.
        log.info("event_notice_sin_conversacion", topic=topic, customer_id=str(customer_id))
        return
    async with get_session() as session:
        customer = await session.get(Customer, customer_id)
    if customer is not None and customer.tenant_id != get_current_tenant_id():
        customer = None  # segundo cinturón además de RLS (auditoría HU-SH-02)
    # `customer.name` lo puede escribir el LLM (`update_customer_profile`): se
    # limpia y se acota antes de que llegue a una plantilla (ADR-0108).
    nombre = clean_param((customer.name if customer else None) or "", 60) or "cliente"
    context = {
        "cliente": {"nombre": nombre},
        "suscripcion": {"concepto": payload.get("concepto")},
        "cargo": {
            "monto": format_amount(payload.get("monto"), payload.get("moneda")),
            "moneda": payload.get("moneda"),
            "periodo": payload.get("periodo"),
            "fecha_vencimiento": payload.get("fecha_vencimiento"),
            "referencia": payload.get("referencia"),
            "link": payload.get("link"),
        },
        "recordatorio": {"dias": _dias_legibles(payload.get("dias"))},
        "mora": {"dias": payload.get("dias_mora")},
        "prueba": {"termina": payload.get("trial_termina")},
        "cita": {
            "titulo": payload.get("titulo"),
            "fecha": payload.get("fecha"),
            "hora": payload.get("hora"),
            "zona": payload.get("zona"),
            "enlace": payload.get("enlace") or "",
        },
    }
    result = await send_event_notice(
        conversation.id,
        topic,
        context,
        notice_key=notice_key_for(topic, payload),
        default_fallback_text=str(payload.get("texto") or ""),
    )
    log.info(
        "event_notice_resultado",
        topic=topic,
        conversation_id=str(conversation.id),
        modo=result.modo,
        detalle=result.detalle,
    )


def _dias_legibles(dias: Any) -> str | None:
    """`-3` → "en 3 días"; `0` → "hoy"; `3` → "hace 3 días"."""
    try:
        n = int(dias)
    except (TypeError, ValueError):
        return None
    if n == 0:
        return "hoy"
    return f"en {abs(n)} días" if n < 0 else f"hace {n} días"


async def on_subscription_charge_created(**payload: Any) -> None:
    await _avisar_suscripcion(ON_SUBSCRIPTION_CHARGE_CREATED, payload)


async def on_subscription_charge_reminder(**payload: Any) -> None:
    await _avisar_suscripcion(ON_SUBSCRIPTION_CHARGE_REMINDER, payload)


async def on_subscription_paused(**payload: Any) -> None:
    await _avisar_suscripcion(ON_SUBSCRIPTION_PAUSED, payload)


async def on_subscription_trial_ending(**payload: Any) -> None:
    await _avisar_suscripcion(ON_SUBSCRIPTION_TRIAL_ENDING, payload)


async def on_segment_send_batch(**payload: Any) -> None:
    from core.admin.envios_segmento import procesar_lote

    await procesar_lote(**payload)


async def on_appointment_notice(topic: str, payload: dict[str, Any]) -> None:
    await _avisar_suscripcion(topic, payload)  # mismo camino: plantilla o texto según la ventana


async def on_appointment_confirmed(**payload: Any) -> None:
    await on_appointment_notice(ON_APPOINTMENT_CONFIRMED, payload)


async def on_appointment_reminder(**payload: Any) -> None:
    await on_appointment_notice(ON_APPOINTMENT_REMINDER, payload)


async def on_appointment_no_show(**payload: Any) -> None:
    await on_appointment_notice(ON_APPOINTMENT_NO_SHOW, payload)


_HANDLERS = (
    (ON_SUBSCRIPTION_CHARGE_CREATED, on_subscription_charge_created),
    (ON_SUBSCRIPTION_CHARGE_REMINDER, on_subscription_charge_reminder),
    (ON_SUBSCRIPTION_PAUSED, on_subscription_paused),
    (ON_SUBSCRIPTION_TRIAL_ENDING, on_subscription_trial_ending),
    (ON_APPOINTMENT_CONFIRMED, on_appointment_confirmed),
    (ON_APPOINTMENT_REMINDER, on_appointment_reminder),
    (ON_APPOINTMENT_NO_SHOW, on_appointment_no_show),
    # HU-SH-03 (ADR-0121): alta automática del negocio de quien compra un plan a un
    # negocio vendedor (allowlist). Para el resto de los negocios no hace nada. Desde
    # HU-SH-04 también sincroniza un cambio de plan del negocio ya creado.
    (ON_SUBSCRIPTION_ACTIVATED, on_subscription_activated),
    # HU-SH-07 (ADR-0125): un alta en revisión aprobada por el master corre acá.
    (ON_ALTA_APROBADA, on_alta_aprobada),
    # HU-SH-04 (ADR-0123): cobro de la plataforma por dos canales con fuente única.
    (ON_SUBSCRIPTION_CHARGE_PAID, on_subscription_charge_paid),
    (ON_PLATFORM_PERIOD_PAID, on_platform_period_paid),
    (ON_PLATFORM_RENEWAL_DUE, on_platform_renewal_due),
    # HU-CRM-06 (ADR-0150): un lote del envío de una plantilla a un segmento de Clientes.
    (ON_SEGMENT_SEND_BATCH, on_segment_send_batch),
)


def ensure_core_handlers() -> None:
    """Suscribe los handlers del core una vez por proceso (después del fork)."""
    global _registered_pid
    pid = os.getpid()
    if _registered_pid == pid:
        return
    event_bus.unsubscribe_all([handler for _, handler in _HANDLERS])
    for topic, handler in _HANDLERS:
        event_bus.subscribe(topic, handler)
    _registered_pid = pid
