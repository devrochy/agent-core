"""EventBus asíncrono in-process.

Desacopla plugins del orchestrator: el core emite eventos, los plugins se
suscriben en `on_activate()`. Todo ocurre en el mismo proceso (monolito),
por lo que un handler es una corrutina Python, no un mensaje de red.

Un handler que falla NUNCA interrumpe el flujo: se loggea y se continúa
(los errores de plugin se aíslan, ver docs/PLUGIN-CONTRACT.md).
"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from core.shared.async_bridge import run_async
from core.shared.logger import get_logger

log = get_logger(__name__)

EventHandler = Callable[..., Awaitable[None] | None]

# --- Eventos del core ---
ON_MESSAGE_RECEIVED = "message.received"          # payload: conversation_id, customer_id, content
ON_MESSAGE_SENT = "message.sent"                  # payload: conversation_id, content
ON_CONVERSATION_STARTED = "conversation.started"  # payload: conversation_id, customer_id
ON_CONVERSATION_CLOSED = "conversation.closed"    # payload: conversation_id
# Motor de procedimientos (smoke de test 2026-10-20): el cliente dio datos de una tarea y el motor
# los aceptó (`actualizar_tarea`). Lo emite después de confirmar la transacción, in-process; cada
# pack filtra SUS `procedure_id` y traduce SUS nombres (p. ej. commerce completa el lead).
# payload: tenant_id, conversation_id, customer_id, procedure_id, datos ({dato: valor}, sin datos de pago).
# Lleva datos personales: los handlers no lo escriben en logs.
ON_PROCEDURE_SLOTS_SET = "procedure.slots_set"
ON_ESCALATION_TRIGGERED = "escalation.triggered"  # payload: conversation_id, reason
ON_PLUGIN_ACTIVATED = "plugin.activated"          # payload: plugin_id
ON_PLUGIN_DISABLED = "plugin.disabled"            # payload: plugin_id, reason
ON_TOOL_EXECUTED = "tool.executed"                # payload: tool_name, plugin_id, conversation_id
# Pagos (ADR-0067): el pack de pagos los emite; otros packs (p. ej. cobranza) se
# suscriben para registrar el cobro. Contrato compartido del core.
ON_PAYMENT_CONFIRMED = "payment.confirmed"        # payload: reference, provider, provider_reference, amount
ON_PAYMENT_REJECTED = "payment.rejected"          # payload: reference, provider, provider_reference, amount
# Suscripciones (HU-ARCH-13, ADR-0108): se emite por el outbox al generar el cargo
# del período; el core avisa al cliente (plantilla fuera de 24 h, texto dentro).
# payload: charge_id, subscription_id, customer_id, concepto, monto, moneda,
#          periodo, fecha_vencimiento, referencia, link
ON_SUBSCRIPTION_CHARGE_CREATED = "subscription.charge_created"
# HU-SUB-04: cargo impago más allá de los días de gracia → suscripción `vencida`.
# payload: charge_id, subscription_id, customer_id, concepto, monto, moneda,
#          periodo, fecha_vencimiento, dias_gracia
ON_SUBSCRIPTION_PAST_DUE = "subscription.past_due"
# HU-SUB-05: recordatorio de un cargo impago (una vez por día configurado) y pausa
# automática por mora. payload: los del cargo + `dias` (recordatorio) o `dias_mora`
# (pausa) y `texto` (respaldo del pack, editable por el negocio).
ON_SUBSCRIPTION_CHARGE_REMINDER = "subscription.charge_reminder"
ON_SUBSCRIPTION_PAUSED = "subscription.paused"
# HU-SUB-06: suscripción activada (alta o cambio de plan) y fin de la prueba de Free
# (oferta de un plan pago). payload: subscription_id, customer_id, concepto, estado,
# referencia (el pedido; None si no hubo pedido), cambio_de_plan y variant_id (la línea del
# pedido). En un negocio vendedor (ADR-0121) dispara el alta automática del comprador.
ON_SUBSCRIPTION_ACTIVATED = "subscription.activated"
# HU-SH-07 (ADR-0125): el master aprobó un alta en revisión; el consumidor la corre en el
# worker. payload: referencia (del alta en el vendedor que emite).
ON_ALTA_APROBADA = "provisioning.alta_aprobada"
# HU-SH-02: citas. Al cliente: confirmación de una cita creada desde el panel,
# recordatorios y oferta de reagendar si no asistió. Al equipo: creada/reagendada/cancelada.
# payload: appointment_id, customer_id, titulo, fecha, hora, zona, enlace, texto…
ON_APPOINTMENT_CONFIRMED = "appointment.confirmed"
ON_APPOINTMENT_REMINDER = "appointment.reminder"
ON_APPOINTMENT_NO_SHOW = "appointment.no_show"
ON_APPOINTMENT_CHANGED = "appointment.changed"
ON_SUBSCRIPTION_TRIAL_ENDING = "subscription.trial_ending"
# HU-CRM-06 (ADR-0150): envío de una plantilla a un segmento de Clientes, por lotes. Cada lote
# es un evento del outbox (`segmento:<envio>:lote:<n>`). payload: send_id, lote.
ON_SEGMENT_SEND_BATCH = "crm.segment_send.batch"
# HU-SH-04 (ADR-0123): cobro de la plataforma por dos canales con fuente única.
# - `subscription.charge_paid`: el proveedor de suscripciones marcó pagado el cargo de un
#   período (en el negocio vendedor). Si la suscripción es la de un negocio aprovisionado, el
#   core aplica ese período a su `tenant_subscriptions`. payload: charge_id, subscription_id,
#   customer_id, periodo, periodo_inicio, periodo_fin, dia_ancla, monto, moneda, variant_id,
#   provider_reference, pago_duplicado.
ON_SUBSCRIPTION_CHARGE_PAID = "subscription.charge_paid"
# - `platform.period_paid`: un período del plan del negocio se pagó por el panel (link de la
#   plataforma). El core marca pagado el cargo del mismo período en el canal asistente
#   (sincronización inversa). payload: referencia, periodo_inicio, periodo_fin, plan_id.
ON_PLATFORM_PERIOD_PAID = "platform.period_paid"
# - `platform.renewal_due`: el job de renovación generó el cobro del período siguiente; el core
#   avisa por el canal preferido del negocio. payload: referencia, plan_id, plan_nombre, monto,
#   moneda, periodo_inicio, periodo_fin.
ON_PLATFORM_RENEWAL_DUE = "platform.renewal_due"
# HU-AOS-07 (cierre por pago, decisión del dueño 2026-10-11): pack-commerce lo escribe en el
# outbox en la misma transacción en que un pedido pasa a `pagado` (ya validado el monto contra
# el total). Cierra las tareas de `contratar_plan` y `tomar_pedido` que esperan el pago, por el
# `order_id` que fijó `crear_pedido`. payload: order_id, public_order_number.
ON_ORDER_PAID = "order.paid"
# HU-AOS-18 (adenda de ADR-0116): la guarda A2A cortó un ciclo (o una autollamada) entre agentes.
# In-process, después de confirmar el evento del ledger. Sin payload de la operación.
# payload: tenant_id, connection_id, conversation_id, call_id, pila (instancias, de la raíz al destino).
ON_A2A_LOOP_DETECTED = "a2a.loop.detected"

# Claves del payload que identifican el objeto de cada evento (HU-AOS-05). El motor de
# procedimientos solo cierra una tarea por estas claves (nunca por montos, proveedores ni el
# cliente): una tarea correlaciona si un dato suyo fijado por una acción tiene el mismo nombre y
# valor. Un topic que no está aquí no cierra tareas (fail-closed).
CLAVES_DE_CORRELACION: dict[str, tuple[str, ...]] = {
    ON_PAYMENT_CONFIRMED: ("reference",),
    ON_PAYMENT_REJECTED: ("reference",),
    ON_SUBSCRIPTION_ACTIVATED: ("subscription_id", "referencia"),
    ON_SUBSCRIPTION_CHARGE_PAID: ("charge_id",),
    ON_APPOINTMENT_CONFIRMED: ("appointment_id",),
    ON_ORDER_PAID: ("order_id",),
}


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)
        # Tasks vivas de emit_nowait: mantener la referencia evita que el
        # recolector se las lleve antes de que terminen.
        self._pending: set[asyncio.Task[None]] = set()

    def subscribe(self, event: str, handler: EventHandler) -> None:
        self._subscribers[event].append(handler)

    def has_subscribers(self, event: str) -> bool:
        return bool(self._subscribers.get(event))

    def unsubscribe_all(self, handlers: list[EventHandler]) -> None:
        """Retira todos los handlers dados (usado al desactivar un plugin)."""
        for event_handlers in self._subscribers.values():
            for h in handlers:
                if h in event_handlers:
                    event_handlers.remove(h)

    async def emit(self, event: str, **payload: Any) -> None:
        for handler in list(self._subscribers.get(event, [])):
            try:
                result = handler(**payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("event_handler_failed", event_name=event, handler=repr(handler))

    async def emit_strict(self, event: str, **payload: Any) -> None:
        """Emite para el despachador del outbox (ADR-0107): si un handler
        falla, levanta (el despachador reintenta con backoff). Corre TODOS los
        handlers y levanta la primera excepción al final."""
        errores: list[Exception] = []
        for handler in list(self._subscribers.get(event, [])):
            try:
                result = handler(**payload)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                log.exception("event_handler_failed", event_name=event, handler=repr(handler))
                errores.append(exc)
        if errores:
            raise errores[0]

    def emit_nowait(self, event: str, **payload: Any) -> None:
        """Emite sin esperar (fire-and-forget) desde contexto síncrono."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            run_async(self.emit(event, **payload))
            return
        # Guardar la referencia: sin ella el recolector puede llevarse la task
        # antes de que termine y el evento se pierde en silencio.
        task = loop.create_task(self.emit(event, **payload))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)


event_bus = EventBus()
