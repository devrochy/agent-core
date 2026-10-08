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
ON_ESCALATION_TRIGGERED = "escalation.triggered"  # payload: conversation_id, reason
ON_PLUGIN_ACTIVATED = "plugin.activated"          # payload: plugin_id
ON_PLUGIN_DISABLED = "plugin.disabled"            # payload: plugin_id, reason
ON_TOOL_EXECUTED = "tool.executed"                # payload: tool_name, plugin_id, conversation_id
# Pagos (ADR-0067): el pack de pagos los emite; otros packs (p. ej. cobranza) se
# suscriben para registrar el cobro. Contrato compartido del core.
ON_PAYMENT_CONFIRMED = "payment.confirmed"        # payload: reference, provider, provider_reference, amount
ON_PAYMENT_REJECTED = "payment.rejected"          # payload: reference, provider, provider_reference, amount


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
