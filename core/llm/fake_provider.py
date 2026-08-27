"""Proveedor LLM FAKE — SOLO modo demo (dev).

Devuelve una respuesta determinística sin red ni API key. El pipeline completo
(webhook → consumer → turno → usage_ledger → panel) corre igual que con un
proveedor real; el objetivo final es una API key real. Prohibido en producción
(ver core/llm/__init__.py).
"""

from __future__ import annotations

from typing import Any

from core.llm.types import LLMResponse, LLMUsageInfo

_DEFAULT_REPLY = (
    "¡Hola! Soy el asistente del negocio. Puedo consultar el catálogo, "
    "armar una cotización o confirmar tu pedido. ¿En qué te ayudo?"
)


class FakeProvider:
    """Proveedor determinístico para DEMO_MODE (dev)."""

    provider_name = "fake"

    def __init__(self, reply: str | None = None) -> None:
        self._reply = reply or _DEFAULT_REPLY

    async def generate(self, **kwargs: Any) -> LLMResponse:
        messages = kwargs.get("messages") or []
        last_user = next(
            (m.content for m in reversed(messages) if getattr(m, "role", None) == "user"),
            "",
        )
        content = self._reply
        if last_user:
            content = f"Recibido: '{last_user[:60]}…'. {content}"
        return LLMResponse(
            content=content,
            stop_reason="end_turn",
            tool_calls=[],
            usage=LLMUsageInfo(input_tokens=10, output_tokens=5),
            model="fake",
            latency_ms=2,
        )