"""Puerto del LLM: la única interfaz que el orchestrator conoce."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from core.llm.types import LLMMessage, LLMResponse, LLMToolSchema


class BaseLLMProvider(ABC):
    provider_name: str = "base"
    model: str = ""

    @abstractmethod
    async def generate(
        self,
        system_blocks: list[str],
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Una llamada completa (no streaming). El agentic loop usa esta ruta."""

    @abstractmethod
    def stream(
        self,
        system_blocks: list[str],
        messages: list[LLMMessage],
        tools: list[LLMToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """Streaming de texto (para canales que lo soporten, ej. web chat)."""

    # Tarifas USD por millón de tokens (input, output) para tracking de costos.
    # Se sobreescriben por proveedor. Fuente: pricing público de cada proveedor.
    pricing_per_mtok: tuple[float, float] = (0.0, 0.0)

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        cin, cout = self.pricing_per_mtok
        return (input_tokens * cin + output_tokens * cout) / 1_000_000
