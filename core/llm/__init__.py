"""Capa de abstracción de LLM.

Cambiar de proveedor = cambiar LLM_PROVIDER en .env (o en el panel admin).
El orchestrator SOLO conoce `BaseLLMProvider`.
"""

from __future__ import annotations

from core.config import get_settings
from core.llm.base import BaseLLMProvider


def resolve_provider(
    provider_key: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> BaseLLMProvider:
    """Resuelve el proveedor activo.

    Prioridad: argumentos explícitos (config de BD/panel) > variables de entorno.
    """
    settings = get_settings()
    key = provider_key or settings.llm_provider
    resolved_api_key = api_key or settings.llm_api_key
    resolved_model = model or settings.llm_model

    if key in ("deepseek", "openai", "openai_compatible"):
        from core.llm.openai_compatible import OpenAICompatibleProvider

        base_urls = {
            "deepseek": "https://api.deepseek.com",
            "openai": "https://api.openai.com",
            "openai_compatible": settings.llm_base_url,
        }
        return OpenAICompatibleProvider(
            api_key=resolved_api_key,
            model=resolved_model,
            base_url=base_urls[key],
            provider_name=key,
        )
    if key == "anthropic":
        from core.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=resolved_api_key, model=resolved_model)

    raise ValueError(f"Proveedor LLM desconocido: {key}")
