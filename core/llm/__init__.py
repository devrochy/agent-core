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
    thinking: bool | None = None,
) -> BaseLLMProvider:
    """Resuelve el proveedor activo.

    Prioridad: argumentos explícitos (config de BD/panel) > variables de entorno.
    DEMO_MODE (solo dev) fuerza un proveedor fake determinístico.
    Con el modo de grabación de las evals encendido (`core.llm.cassette.activar`, solo lo
    activan los runners de evals), el proveedor se envuelve para grabar o reproducir; en
    `replay` el proveedor real ni siquiera se construye.

    `thinking`: para DeepSeek V4, `False` para el turno normal y `True` para
    razonamiento. `None` deja el default del proveedor (OpenAI ignora el campo).
    """
    settings = get_settings()
    from core.llm import cassette

    modo = cassette.modo_activo()

    if settings.demo_mode:
        if settings.app_env != "development":
            raise ValueError("DEMO_MODE solo está permitido en APP_ENV=development")
        log.warning(
            "llm_fake_mode",
            note="respuestas determinísticas (demo). Producción usa API key real.",
        )
        from core.llm.fake_provider import FakeProvider

        if modo == "off":
            return FakeProvider()  # type: ignore[return-value]
        return cassette.ProveedorGrabado(
            modo=modo,
            inner=FakeProvider() if modo == "record" else None,
            provider_name="fake",
            model="fake",
            thinking=None,
            pricing_per_mtok=(0.0, 0.0),
        )

    key = provider_key or settings.llm_provider
    resolved_api_key = api_key or settings.llm_api_key
    resolved_model = model or settings.llm_model
    if thinking is None and key in ("deepseek", "openai_compatible"):
        # En DeepSeek, un ID explícito (V4) enciende thinking por defecto; el
        # turno normal debe apagarlo. `.env`/panel pueden sobreescribir.
        thinking = settings.llm_thinking_default

    if key in ("deepseek", "openai", "openai_compatible"):
        from core.llm.openai_compatible import PRICING, resolve_thinking_for_model

        resolved_thinking = (
            resolve_thinking_for_model(resolved_model, bool(thinking)) if thinking is not None else None
        )
        pricing = PRICING.get(resolved_model, (0.0, 0.0))
    elif key == "anthropic":
        from core.llm.anthropic_provider import PRICING as ANTHROPIC_PRICING

        resolved_thinking = None
        pricing = ANTHROPIC_PRICING.get(resolved_model, (3.00, 15.00))
    else:
        raise ValueError(f"Proveedor LLM desconocido: {key}")

    if modo == "replay":
        # Evals en replay (adenda de ADR-0119): NO se construye el proveedor real, así que
        # no hay forma de salir a la red ni de necesitar LLM_API_KEY.
        return cassette.ProveedorGrabado(
            modo="replay",
            inner=None,
            provider_name=key,
            model=resolved_model,
            thinking=resolved_thinking,
            pricing_per_mtok=pricing,
        )

    provider: BaseLLMProvider
    if key == "anthropic":
        from core.llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(api_key=resolved_api_key, model=resolved_model)
    else:
        from core.llm.openai_compatible import OpenAICompatibleProvider

        base_urls = {
            "deepseek": "https://api.deepseek.com",
            "openai": "https://api.openai.com",
            "openai_compatible": settings.llm_base_url,
        }
        provider = OpenAICompatibleProvider(
            api_key=resolved_api_key,
            model=resolved_model,
            base_url=base_urls[key],
            provider_name=key,
            thinking=resolved_thinking,
        )
    if modo == "record":
        if not resolved_api_key:
            raise cassette.ErrorGrabacion("Grabar (record) necesita LLM_API_KEY: sin clave no se graba nada.")
        return cassette.ProveedorGrabado(
            modo="record",
            inner=provider,
            provider_name=key,
            model=resolved_model,
            thinking=resolved_thinking,
            pricing_per_mtok=provider.pricing_per_mtok,
            secretos=(resolved_api_key,),
        )
    return provider
