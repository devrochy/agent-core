"""Puerto del LLM: la única interfaz que el orchestrator conoce."""

from __future__ import annotations

import inspect
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
    # USD por millón de tokens (lectura, escritura) de la caché (ADR-0208 §13). None = el modelo no
    # publica precio de caché: esos tokens se cobran a precio de entrada (sin descuento ni recargo).
    cache_pricing_per_mtok: tuple[float, float] | None = None
    # Si `input_tokens` ya incluye lo leído y escrito en caché (OpenAI-compatible sí, Anthropic no).
    input_includes_cache: bool = True

    def estimate_cost_usd(
        self,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> float:
        """Costo USD de una respuesta. `cached_tokens` (leídos de la caché) y `cache_write_tokens`
        se cobran a su precio de caché (ADR-0208 §13); sin ellos, el cálculo de siempre."""
        cin, cout = self.pricing_per_mtok
        lectura, escritura = self.cache_pricing_per_mtok or (cin, cin)
        cached_tokens, cache_write_tokens = max(0, cached_tokens), max(0, cache_write_tokens)
        if self.input_includes_cache:
            # Lo cacheado ya viene dentro de `input_tokens`: se descuenta de la parte a precio pleno.
            sin_cache = max(0, input_tokens - cached_tokens - cache_write_tokens)
        else:
            sin_cache = input_tokens
        return (
            sin_cache * cin + cached_tokens * lectura + cache_write_tokens * escritura + output_tokens * cout
        ) / 1_000_000


def costo_de_respuesta(provider: object, usage: object) -> float:
    """Costo USD de una respuesta con su caché (ADR-0208 §13), según el proveedor que la sirvió.

    Un `estimate_cost_usd` que solo acepta `(entrada, salida)` (dobles de prueba, envoltorios
    viejos) se llama con esos dos, como antes: sin caché no hay nada más que pasarle."""
    entrada = int(getattr(usage, "input_tokens", 0) or 0)
    salida = int(getattr(usage, "output_tokens", 0) or 0)
    leidos = int(getattr(usage, "cache_read_tokens", 0) or 0)
    escritos = int(getattr(usage, "cache_write_tokens", 0) or 0)
    estimar = provider.estimate_cost_usd  # type: ignore[attr-defined]
    if (leidos or escritos) and _acepta_cache(estimar):
        return float(estimar(entrada, salida, cached_tokens=leidos, cache_write_tokens=escritos))
    return float(estimar(entrada, salida))


def _acepta_cache(estimar: object) -> bool:
    try:
        parametros = inspect.signature(estimar).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return "cached_tokens" in parametros or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parametros.values()
    )
