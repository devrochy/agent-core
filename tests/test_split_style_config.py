"""HU-ARCH-11: el default global del estilo de envío es `un_mensaje`."""

from __future__ import annotations


def test_default_global_es_un_mensaje(monkeypatch) -> None:
    monkeypatch.delenv("MESSAGE_SPLIT_STYLE", raising=False)
    from core.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.message_split_style == "un_mensaje"
