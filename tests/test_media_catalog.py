from core.media.catalog import (
    AUDIO_CATALOG,
    VISION_CATALOG,
    audio_cost_usd,
    audio_model_or_default,
    audio_price_per_minute,
    is_audio_provider,
    is_vision_mode,
    is_vision_provider,
    vision_cost_usd,
    vision_model_or_default,
    vision_price_per_image,
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


def test_catalogo_expone_calidad_tier_y_legado() -> None:
    audio = {m.id: m for m in AUDIO_CATALOG["openai"].models}
    assert audio["gpt-4o-transcribe"].quality == 5
    assert audio["gpt-4o-transcribe"].tier == "maxima"
    assert not audio["gpt-4o-transcribe"].legacy
    assert audio["gpt-4o-mini-transcribe"].offered is False
    assert audio["whisper-1"].legacy
    assert audio["whisper-1"].offered is False

    vision = {m.id: m for m in VISION_CATALOG["openai"].models}
    assert vision["gpt-4.1-mini"].quality == 4
    assert vision["gpt-4.1-mini"].tier == "preciso"
    assert not vision["gpt-4.1-mini"].legacy
    assert vision["gpt-4o"].legacy


def test_costos_de_referencia_por_unidad() -> None:
    assert audio_price_per_minute("openai", "gpt-4o-transcribe") == 0.006
    # 1.200 tokens de entrada + 300 de salida: 0,00048 + 0,00048.
    assert vision_price_per_image("openai", "gpt-4.1-mini") == 0.00096
