"""Reglas y síntesis del pack de voz (ADR-0197)."""

from __future__ import annotations

import uuid

import pytest
from pack_voice import module as voice_module
from pack_voice import policy, tts
from pack_voice.config import VoiceConfig

from core.mcp.host import MCPHost
from core.mcp.types import VoiceSynthesis
from core.media.tts import SpeechResult


def _config(**overrides) -> VoiceConfig:  # type: ignore[no-untyped-def]
    base = {
        "enabled": True,
        "modo": "mixto",
        "solo_si_cliente_manda_audio": True,
        "probabilidad_voz": 100,
        "evitar_voz_seguida": True,
        "incluir_texto": True,
        "max_caracteres": 600,
    }
    base.update(overrides)
    return VoiceConfig(**base)


def test_modo_nunca_o_apagado_no_usa_voz() -> None:
    assert not policy.decidir_por_reglas(
        _config(modo="nunca"), texto="hola", es_audio_entrante=True, voz_anterior=False, azar=1
    )
    assert not policy.decidir_por_reglas(
        _config(enabled=False), texto="hola", es_audio_entrante=True, voz_anterior=False, azar=1
    )


def test_solo_responde_con_voz_si_el_cliente_mando_audio() -> None:
    config = _config()
    assert not policy.decidir_por_reglas(
        config, texto="hola", es_audio_entrante=False, voz_anterior=False, azar=1
    )
    assert policy.decidir_por_reglas(
        config, texto="hola", es_audio_entrante=True, voz_anterior=False, azar=1
    )


def test_evita_dos_voces_seguidas() -> None:
    assert not policy.decidir_por_reglas(
        _config(), texto="hola", es_audio_entrante=True, voz_anterior=True, azar=1
    )


def test_respuesta_larga_sale_solo_en_texto() -> None:
    assert not policy.decidir_por_reglas(
        _config(max_caracteres=50),
        texto="x" * 51,
        es_audio_entrante=True,
        voz_anterior=False,
        azar=1,
    )


def test_probabilidad_del_modo_mixto() -> None:
    config = _config(probabilidad_voz=50)
    assert policy.decidir_por_reglas(
        config, texto="hola", es_audio_entrante=True, voz_anterior=False, azar=50
    )
    assert not policy.decidir_por_reglas(
        config, texto="hola", es_audio_entrante=True, voz_anterior=False, azar=51
    )


def test_modo_siempre_no_sortea() -> None:
    assert policy.decidir_por_reglas(
        _config(modo="siempre", probabilidad_voz=0),
        texto="hola",
        es_audio_entrante=True,
        voz_anterior=False,
        azar=100,
    )


async def test_decidir_lee_el_estado_y_aplica_las_reglas(monkeypatch: pytest.MonkeyPatch) -> None:
    voz = _config()

    async def _cargar(**_: object) -> VoiceConfig:
        return voz

    async def _estado(_conversation_id: uuid.UUID):
        return True, True, False

    monkeypatch.setattr(policy, "cargar_config", _cargar)
    monkeypatch.setattr(policy, "_estado_de_la_conversacion", _estado)

    plan = await policy.decidir(uuid.uuid4(), "hola")
    assert plan is not None
    assert plan.include_text is True


async def test_decidir_sin_conversacion_waweb_no_usa_voz(monkeypatch: pytest.MonkeyPatch) -> None:
    voz = _config()

    async def _cargar(**_: object) -> VoiceConfig:
        return voz

    async def _estado(_conversation_id: uuid.UUID):
        return None

    monkeypatch.setattr(policy, "cargar_config", _cargar)
    monkeypatch.setattr(policy, "_estado_de_la_conversacion", _estado)

    assert await policy.decidir(uuid.uuid4(), "hola") is None


async def test_sintetizar_guarda_ogg_y_devuelve_url(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()

    async def _cargar(**_: object) -> VoiceConfig:
        return config

    async def _audio(*_a, **_k) -> SpeechResult:
        return SpeechResult(contenido=b"OggSdatos")

    async def _slug() -> str:
        return "demo"

    def _guardar(*_a, **_k):
        from types import SimpleNamespace

        return SimpleNamespace(key="demo/media/salientes/voz.ogg")

    class _Settings:
        openai_api_key = "sk-test"
        tts_timeout_s = 60.0

    monkeypatch.setattr(tts, "cargar_config", _cargar)
    monkeypatch.setattr(tts, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tts, "sintetizar_audio", _audio)
    monkeypatch.setattr(tts, "current_tenant_slug", _slug)
    monkeypatch.setattr(tts, "guardar", _guardar)
    monkeypatch.setattr(tts, "url", lambda key, ttl_s=0: f"https://api/files/{key}")

    sintesis = await tts.sintetizar(uuid.uuid4(), "hola")

    assert sintesis is not None
    assert sintesis.audio_key == "demo/media/salientes/voz.ogg"
    assert sintesis.mimetype == "audio/ogg"


async def test_sintetizar_sin_llave_no_llama_a_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config()

    async def _cargar(**_: object) -> VoiceConfig:
        return config

    async def _audio(*_a, **_k) -> SpeechResult:
        raise AssertionError("no debe llamar sin llave")

    monkeypatch.setattr(tts, "cargar_config", _cargar)
    monkeypatch.setattr(tts, "sintetizar_audio", _audio)
    monkeypatch.setattr(tts, "get_settings", lambda: type("S", (), {"openai_api_key": ""})())

    assert await tts.sintetizar(uuid.uuid4(), "hola") is None


async def test_module_registra_y_retira_los_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    host = MCPHost()

    async def _poli(_cid: uuid.UUID, _texto: str) -> bool:
        return True

    async def _synth(_cid: uuid.UUID, _texto: str) -> VoiceSynthesis:
        return VoiceSynthesis(audio_key="k", audio_url="https://api/files/k.ogg")

    monkeypatch.setattr(voice_module, "decidir", _poli)
    monkeypatch.setattr(voice_module, "sintetizar", _synth)
    host.begin_activation("pack_voice")
    voice_module.VoiceModule().on_activate(host)
    host.end_activation()

    plan = await host.resolve_voice(uuid.uuid4(), "hola")
    assert plan is not None and plan.plugin_id == "pack_voice"
    sintesis = await host.synthesize_voice(uuid.uuid4(), "hola")
    assert sintesis is not None and sintesis.audio_key == "k"

    host.remove_plugin("pack_voice")
    assert await host.resolve_voice(uuid.uuid4(), "hola") is None
    assert await host.synthesize_voice(uuid.uuid4(), "hola") is None
