"""Capa de abstracción de LLM.

Cambiar de proveedor = cambiar LLM_PROVIDER en .env (o en el panel admin).
El orchestrator SOLO conoce `BaseLLMProvider`.
"""

from __future__ import annotations

from core.config import get_settings
from core.llm.base import BaseLLMProvider
from core.shared.logger import get_logger

log = get_logger(__name__)


def resolve_provider(
    provider_key: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> BaseLLMProvider:
    """Resuelve el proveedor activo.

    Prioridad: argumentos explícitos (config de BD/panel) > variables de entorno.
    DEMO_MODE (solo dev) fuerza un proveedor fake determinístico.
    """
    settings = get_settings()

    if settings.demo_mode:
        if settings.app_env != "development":
            raise ValueError("DEMO_MODE solo está permitido en APP_ENV=development")
        log.warning(
            "llm_fake_mode",
            note="respuestas determinísticas (demo). Producción usa API key real.",
        )
        from core.llm.fake_provider import FakeProvider

        return FakeProvider()

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
