"""Guard de arranque de `APP_SECRET_KEY` (evaluación 2026-11-09, punto 1)."""

from __future__ import annotations

import pytest

from core.config import ClaveSecretaInsegura, Settings, verificar_clave_secreta

CLAVE_BUENA = "a" * 64


def _ajustes(monkeypatch: pytest.MonkeyPatch, **entorno: str) -> Settings:
    for nombre, valor in entorno.items():
        monkeypatch.setenv(nombre, valor)
    return Settings(_env_file=None)


@pytest.mark.parametrize("clave", ["change-me", "", "  ", "CHANGE-ME", "ci-only-not-a-secret-app-key", "x" * 31])
def test_produccion_rechaza_clave_de_ejemplo_o_corta(monkeypatch: pytest.MonkeyPatch, clave: str) -> None:
    ajustes = _ajustes(monkeypatch, APP_ENV="production", APP_SECRET_KEY=clave)
    with pytest.raises(ClaveSecretaInsegura, match="openssl rand -hex 32"):
        verificar_clave_secreta(ajustes)


def test_produccion_sin_variable_usa_el_default_y_falla(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    ajustes = _ajustes(monkeypatch, APP_ENV="production")
    with pytest.raises(ClaveSecretaInsegura):
        verificar_clave_secreta(ajustes)


def test_produccion_acepta_clave_de_32_o_mas(monkeypatch: pytest.MonkeyPatch) -> None:
    verificar_clave_secreta(_ajustes(monkeypatch, APP_ENV="production", APP_SECRET_KEY="b" * 32))
    verificar_clave_secreta(_ajustes(monkeypatch, APP_ENV="production", APP_SECRET_KEY=CLAVE_BUENA))


def test_desarrollo_no_exige_nada(monkeypatch: pytest.MonkeyPatch) -> None:
    verificar_clave_secreta(_ajustes(monkeypatch, APP_ENV="development", APP_SECRET_KEY="change-me"))


def test_el_mensaje_no_incluye_la_clave(monkeypatch: pytest.MonkeyPatch) -> None:
    ajustes = _ajustes(monkeypatch, APP_ENV="production", APP_SECRET_KEY="corta-pero-secreta")
    with pytest.raises(ClaveSecretaInsegura) as error:
        verificar_clave_secreta(ajustes)
    assert "corta-pero-secreta" not in str(error.value)
