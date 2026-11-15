"""Síntesis de voz de salida del core (ADR-0197)."""

from __future__ import annotations

import httpx
import pytest

from core.media.errors import MediaProviderError
from core.media.tts import OPENAI_SPEECH_URL, sintetizar_audio


async def test_openai_devuelve_el_audio_ogg() -> None:
    visto: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        visto.append(request)
        return httpx.Response(200, content=b"OggSaudio")

    resultado = await sintetizar_audio(
        "hola",
        model="tts-1",
        voice="alloy",
        api_key="sk-test",
        transport=httpx.MockTransport(handler),
    )

    assert resultado.contenido == b"OggSaudio"
    assert resultado.mimetype == "audio/ogg"
    assert str(visto[0].url) == OPENAI_SPEECH_URL
    assert visto[0].headers["authorization"] == "Bearer sk-test"


async def test_error_del_proveedor_se_clasifica() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limit")

    with pytest.raises(MediaProviderError) as exc:
        await sintetizar_audio(
            "hola",
            model="tts-1",
            voice="alloy",
            api_key="sk-test",
            transport=httpx.MockTransport(handler),
        )

    assert exc.value.transient is True


async def test_sin_llave_no_sale_a_red() -> None:
    with pytest.raises(MediaProviderError, match="llave"):
        await sintetizar_audio("hola", model="tts-1", voice="alloy", api_key="")
