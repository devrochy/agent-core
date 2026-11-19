"""HU-ARCH-10: modelos DeepSeek vigentes y verificados (P-05/D-22).

`deepseek-chat` y `deepseek-reasoner` se discontinuaron el 2026-07-24. El
framework debe usar `deepseek-flash`, con `thinking` explícito, y costearlo.
"""

from __future__ import annotations

import pytest

from core.llm.openai_compatible import (
    PRICING,
    RETIRED_DEEPSEEK_MODELS,
    OpenAICompatibleProvider,
    resolve_thinking_for_model,
)


def test_defaults_no_usan_modelos_discontinuados(monkeypatch: pytest.MonkeyPatch) -> None:
    # El default del campo (sin variables de entorno) debe ser vigente; el
    # entorno puede sobreescribirlo mientras se migra, nunca por defecto.
    for var in ("LLM_MODEL", "LLM_MODEL_REASONING", "LLM_MODEL_FALLBACK"):
        monkeypatch.delenv(var, raising=False)
    from core.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_model not in RETIRED_DEEPSEEK_MODELS
    assert settings.llm_model == "deepseek-flash"
    assert settings.llm_model_reasoning not in RETIRED_DEEPSEEK_MODELS
    assert settings.llm_model_fallback not in RETIRED_DEEPSEEK_MODELS


def test_precio_de_deepseek_flash_no_es_cero() -> None:
    provider = OpenAICompatibleProvider(
        api_key="x", model="deepseek-flash", base_url="https://api.deepseek.com"
    )
    assert provider.pricing_per_mtok == PRICING["deepseek-flash"]
    assert provider.pricing_per_mtok[0] > 0
    assert provider.pricing_per_mtok[1] > 0


@pytest.mark.parametrize(
    ("model", "thinking", "expected"),
    [
        ("deepseek-flash", False, False),
        ("deepseek-flash", True, True),
        ("deepseek-v4-pro", True, True),
    ],
)
def test_thinking_se_respeta_para_modelos_vigentes(
    model: str, thinking: bool, expected: bool
) -> None:
    assert resolve_thinking_for_model(model, thinking) is expected


def test_alias_discontinuados_mapean_su_modo_historico() -> None:
    # Aunque estén retirados, se preserva el modo para no romper en seco.
    assert resolve_thinking_for_model("deepseek-chat", False) is False
    assert resolve_thinking_for_model("deepseek-reasoner", False) is True


def test_payload_thinking_solo_en_deepseek() -> None:
    deepseek = OpenAICompatibleProvider(
        api_key="x", model="deepseek-flash", base_url="https://api.deepseek.com",
        provider_name="deepseek", thinking=False,
    )
    assert deepseek._thinking_payload() == {"thinking": {"type": "disabled"}}

    openai = OpenAICompatibleProvider(
        api_key="x", model="gpt-4o-mini", base_url="https://api.openai.com",
        provider_name="openai", thinking=None,
    )
    assert openai._thinking_payload() == {}
