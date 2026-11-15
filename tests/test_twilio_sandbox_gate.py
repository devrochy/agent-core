"""HU-CONN-20 · gate del Sandbox de Twilio: la variable de ambiente se lee en un solo lugar y se cierra por defecto."""

from __future__ import annotations

from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PERMITIDOS = {RAIZ / "core" / "config.py", RAIZ / "core" / "gateway" / "twilio" / "sandbox.py"}


def test_la_variable_solo_se_lee_en_sandbox_py():
    """Leerla en otro lado saltaría el gate (ruta, servicio, webhook y salida pasan por `sandbox.permitido()`)."""
    culpables = []
    for carpeta in ("core", "packages", "plugins", "cli"):
        for archivo in (RAIZ / carpeta).rglob("*.py"):
            if archivo in PERMITIDOS:
                continue
            contenido = archivo.read_text(encoding="utf-8", errors="ignore")
            if "twilio_sandbox_permitido" in contenido or "TWILIO_SANDBOX_PERMITIDO" in contenido:
                culpables.append(str(archivo.relative_to(RAIZ)))
    assert culpables == []


def test_apagado_por_defecto(monkeypatch):
    from core.config import Settings

    monkeypatch.delenv("TWILIO_SANDBOX_PERMITIDO", raising=False)
    campo = Settings.model_fields["twilio_sandbox_permitido"]
    assert campo.default is False
    assert Settings.model_fields["twilio_sandbox_e164"].default == "+14155238886"


@pytest.mark.parametrize("valor", [True, False])
def test_permitido_e_inerte(monkeypatch, valor):
    from core.config import get_settings
    from core.gateway.twilio import sandbox

    monkeypatch.setattr(get_settings(), "twilio_sandbox_permitido", valor)
    assert sandbox.permitido() is valor
    assert sandbox.inerte("sandbox") is (not valor)
    assert sandbox.inerte("sender") is False


@pytest.mark.parametrize(
    ("codigo", "ok"),
    [
        ("cloud-forgotten", True),
        ("abc", True),
        ("a1-b2-c3", True),
        ("Cloud", False),
        ("a b", False),
        ("-x", False),
        ("x-", False),
        ("a" * 65, False),
        ("", False),
    ],
)
def test_formato_del_join_code(codigo, ok):
    from core.gateway.twilio import sandbox

    assert sandbox.join_code_valido(codigo) is ok


def test_external_id():
    from core.gateway.twilio import sandbox

    assert sandbox.external_id("AC" + "1" * 32) == "sandbox:AC" + "1" * 32
