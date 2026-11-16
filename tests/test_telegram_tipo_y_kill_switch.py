"""Canal Telegram (ADR-0199, PR core 1): tipo en el catálogo, capacidades y kill-switch `TELEGRAM_HABILITADO`."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.gateway.connection_types import (
    BY_ID,
    CAPACIDADES_POR_TIPO,
    CapacidadesCanal,
    capacidades_de,
    disponible_para,
    tipo_de_conexion,
    types_by_category,
)

RAIZ = Path(__file__).resolve().parents[1]
PERMITIDOS = {RAIZ / "core" / "config.py", RAIZ / "core" / "gateway" / "telegram" / "habilitado.py"}


def test_la_variable_solo_se_lee_en_habilitado_py():
    """Leerla en otro lado saltaría el kill-switch (rutas, webhook, salida y cron pasan por `habilitado()`)."""
    culpables = []
    for carpeta in ("core", "packages", "plugins", "cli"):
        for archivo in (RAIZ / carpeta).rglob("*.py"):
            if archivo in PERMITIDOS:
                continue
            contenido = archivo.read_text(encoding="utf-8", errors="ignore")
            if "telegram_habilitado" in contenido or "TELEGRAM_HABILITADO" in contenido:
                culpables.append(str(archivo.relative_to(RAIZ)))
    assert culpables == []


def test_encendido_por_defecto(monkeypatch):
    from core.config import Settings

    monkeypatch.delenv("TELEGRAM_HABILITADO", raising=False)
    assert Settings.model_fields["telegram_habilitado"].default is True


@pytest.mark.parametrize(("valor", "esperado"), [(True, True), (False, False), ("true", False), (1, False)])
def test_solo_el_booleano_true_lo_enciende(monkeypatch, valor, esperado):
    from core.config import get_settings
    from core.gateway.telegram import habilitado

    monkeypatch.setattr(get_settings(), "telegram_habilitado", valor)
    assert habilitado.habilitado() is esperado


def test_tipo_telegram_disponible_para_todos():
    tipo = BY_ID["telegram"]
    assert (tipo.channel, tipo.provider, tipo.category) == ("telegram", "telegram", "comunicacion")
    assert tipo.produces_conversations is True
    # Sin bandera por negocio (D-74): visible en el selector desde el PR core 4, cuando ya entra y sale texto.
    assert tipo.bandera_negocio == ""
    assert tipo.available is True and tipo.en_selector is True
    assert any(i.id == "telegram" for g in types_by_category() for i in g["items"])
    # Credenciales: el token y el secreto del webhook de solo escritura; el `bot_id` (de `getMe`) es la clave de ruteo.
    props = tipo.credentials_schema["properties"]
    assert set(props) == {"bot_token", "bot_id", "webhook_secret"}
    assert props["bot_token"]["writeOnly"] is True and props["webhook_secret"]["writeOnly"] is True
    assert tipo.external_id_key == "bot_id"


def test_entra_al_catalogo_de_mensajes_con_botones():
    from core.gateway.connection_types import canales_con

    assert "telegram" in canales_con("botones")


def test_capacidades_de_telegram():
    assert CAPACIDADES_POR_TIPO["telegram"] == CapacidadesCanal(botones=True, lista=True, voz=True, pedir_contacto=True)
    assert capacidades_de("telegram", "telegram").ventana_24h is False
    assert capacidades_de("telegram").plantilla_aprobada is False
    assert tipo_de_conexion("telegram") == "telegram"


def test_el_kill_switch_va_antes_que_available(monkeypatch):
    """`available=True` desde el PR core 4: el kill-switch apagado lo sigue cerrando."""
    from core.config import get_settings

    tipo = BY_ID["telegram"]
    monkeypatch.setattr(get_settings(), "telegram_habilitado", True)
    assert disponible_para(tipo, {}) is True
    monkeypatch.setattr(get_settings(), "telegram_habilitado", False)
    assert disponible_para(tipo, {}) is False
    # El kill-switch no toca a los demás tipos.
    assert disponible_para(BY_ID["whatsapp_meta"], {}) is True
