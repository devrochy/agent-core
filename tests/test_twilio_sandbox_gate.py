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


class _Registro:
    def __init__(self) -> None:
        self.puntos: list[tuple[float, dict[str, str]]] = []

    def add(self, valor: float, atributos: dict[str, str] | None = None) -> None:
        self.puntos.append((valor, dict(atributos or {})))

    def set(self, valor: float, atributos: dict[str, str] | None = None) -> None:
        self.puntos.append((valor, dict(atributos or {})))


@pytest.fixture
def eventos(monkeypatch):
    from core.gateway.twilio import sandbox

    registro = _Registro()
    monkeypatch.setattr(sandbox, "_eventos", registro)
    return registro


def test_contar_usa_etiquetas_cerradas(eventos):
    from core.gateway.twilio import sandbox

    sandbox.contar("no_unido")
    sandbox.contar("+573001234567")
    assert eventos.puntos == [(1, {"evento": "no_unido"}), (1, {"evento": "otro"})]


def test_una_metrica_rota_no_rompe_el_camino(monkeypatch):
    from core.gateway.twilio import sandbox

    class _Rota:
        def add(self, *_a, **_k):
            raise RuntimeError("x")

        set = add

    monkeypatch.setattr(sandbox, "_eventos", _Rota())
    sandbox.contar("no_unido")


def test_medir_el_envio_del_sandbox(eventos):
    import uuid

    from core.gateway.channels.base import ProviderError
    from core.tasks.outbound_send import _medir_sandbox

    del_sandbox = {"from": "whatsapp:+14155238886"}
    propio = {"from": "whatsapp:+15550001111"}
    tid = uuid.uuid4()
    _medir_sandbox(
        ProviderError("x", provider="twilio", code=63015, category="PERMANENT"), del_sandbox, tid
    )
    # Desde un número propio no se mide nada: ni el 63015 ni el ritmo son del sandbox.
    _medir_sandbox(
        ProviderError("x", provider="twilio", code=63015, category="PERMANENT"), propio, tid
    )
    _medir_sandbox(
        ProviderError("x", provider="twilio", code=63018, category="RATE_LIMIT"), del_sandbox, tid
    )
    _medir_sandbox(
        ProviderError("x", provider="twilio", code=63018, category="RATE_LIMIT"), propio, tid
    )
    assert [p[1]["evento"] for p in eventos.puntos] == ["no_unido", "reintento_ritmo"]


def test_medir_nunca_cambia_el_error(monkeypatch):
    import uuid

    from core.gateway.channels.base import ProviderError
    from core.gateway.twilio import sandbox
    from core.tasks.outbound_send import _medir_sandbox

    def _rompe(_c):
        raise RuntimeError("x")

    monkeypatch.setattr(sandbox, "es_from_del_sandbox", _rompe)
    _medir_sandbox(
        ProviderError("x", provider="twilio", code=63018, category="RATE_LIMIT"), {}, uuid.uuid4()
    )
