from core.media.catalog import (
    AUDIO_CATALOG,
    VISION_CATALOG,
    audio_cost_usd,
    audio_model_or_default,
    is_audio_provider,
    is_vision_mode,
    is_vision_provider,
    vision_cost_usd,
    vision_model_or_default,
)


def test_catalogos_tienen_los_defaults_del_dueno() -> None:
    assert AUDIO_CATALOG["groq"].default_model == "whisper-large-v3-turbo"
    assert VISION_CATALOG["deepseek"].default_model == "deepseek-flash"


def test_valida_proveedores_y_modelos() -> None:
    assert is_audio_provider("groq")
    assert is_audio_provider("openai")
    assert not is_audio_provider("anthropic")
    assert is_vision_provider("deepseek")
    assert is_vision_provider("anthropic")
    assert is_vision_provider("openai")
    assert not is_vision_provider("groq")
    assert is_vision_mode("describir")
    assert is_vision_mode("sin_descripcion")
    assert not is_vision_mode("otro")


def test_modelo_desconocido_cae_al_default() -> None:
    assert audio_model_or_default("groq", "no-existe") == "whisper-large-v3-turbo"
    assert vision_model_or_default("deepseek", "no-existe") == "deepseek-flash"


def test_costos_de_referencia() -> None:
    # Groq turbo: USD 0,04 por hora -> ~0,000667 por minuto.
    assert 0 < audio_cost_usd("groq", "whisper-large-v3-turbo", 60) < 0.001
    assert audio_cost_usd("groq", "whisper-large-v3-turbo", 0) == 0
    assert vision_cost_usd("deepseek", "deepseek-flash", 1_000_000, 0) == 0.14
    assert vision_cost_usd("deepseek", "deepseek-flash", 0, 1_000_000) == 0.28
