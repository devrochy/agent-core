"""HU-CRM-06 · piezas puras del envío a un segmento (ADR-0150)."""

from __future__ import annotations

import uuid

import pytest

from core.admin import envios_segmento as envios


@pytest.mark.parametrize(
    "texto",
    ["BAJA", "  Stop ", "No más promociones!", "no quiero mas promociones", "Cancelar promociones."],
)
def test_palabras_de_baja_por_defecto(texto: str) -> None:
    assert envios.es_palabra_de_baja(texto)


@pytest.mark.parametrize("texto", ["baja el precio", "¿hay promociones?", "stop por favor llámame", ""])
def test_solo_el_mensaje_entero_da_de_baja(texto: str) -> None:
    assert not envios.es_palabra_de_baja(texto)


def test_palabras_de_baja_del_negocio_reemplazan_las_de_defecto() -> None:
    assert envios.es_palabra_de_baja("Salir", ["salir"])
    assert not envios.es_palabra_de_baja("baja", ["salir"])


def test_id_estable_por_envio_y_cliente() -> None:
    envio, cliente = uuid.uuid4(), uuid.uuid4()
    cmid = envios.client_message_id(envio, cliente)
    assert cmid == envios.client_message_id(envio, cliente)
    assert envios.es_de_un_envio(cmid)
    assert not envios.es_de_un_envio("sub_charge:123")
    assert not envios.es_de_un_envio(None)


@pytest.mark.parametrize(
    ("tier", "limite"),
    [("TIER_250", 250), ("TIER_1K", 1000), ("tier_10k", 10000), ("TIER_UNLIMITED", None), (None, None)],
)
def test_limite_del_tier(tier: str | None, limite: int | None) -> None:
    assert envios.limite_del_tier(tier) == limite


def test_variables_fijas_y_del_nombre() -> None:
    variables = envios.validar_variables(
        ["1", "2"],
        {"1": {"fuente": "cliente.nombre", "valor": "amigo"}, "2": {"fuente": "fijo", "valor": " 20%\n en todo "}},
    )
    assert variables["2"]["valor"] == "20% en todo"
    assert envios.valores_para(variables, "Ana") == {"1": "Ana", "2": "20% en todo"}
    assert envios.valores_para(variables, None) == {"1": "amigo", "2": "20% en todo"}
    # El nombre lo puede escribir el agente: se acota (ADR-0108).
    assert len(envios.valores_para(variables, "x" * 500)["1"]) == envios.MAX_NOMBRE  # type: ignore[index]


def test_sin_nombre_y_sin_respaldo_no_hay_valores() -> None:
    variables = envios.validar_variables(["1"], {"1": {"fuente": "cliente.nombre", "valor": ""}})
    assert envios.valores_para(variables, "") is None


@pytest.mark.parametrize(
    "variables",
    [
        {"1": {"fuente": "fijo", "valor": "a"}},  # falta la 2
        {"1": {"fuente": "fijo", "valor": "a"}, "2": {"fuente": "fijo", "valor": "  "}},  # vacía
        {"1": {"fuente": "fijo", "valor": "a"}, "2": {"fuente": "llm", "valor": "x"}},  # fuente no permitida
    ],
)
def test_variables_invalidas(variables: dict[str, dict[str, str]]) -> None:
    with pytest.raises(envios.EnvioError) as exc:
        envios.validar_variables(["1", "2"], variables)
    assert exc.value.code == "template_variables"


def test_guarda_de_test(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("QA_TESTERS_ENABLED", "false")
    get_settings.cache_clear()
    try:
        assert envios.lista_permitida() is None  # producción real: sin guarda
        monkeypatch.setenv("QA_TESTERS_ENABLED", "true")
        monkeypatch.setenv("SEGMENT_SEND_ALLOWLIST", "+57 300 111 2222, 573004445555")
        get_settings.cache_clear()
        assert envios.lista_permitida() == {"573001112222", "573004445555"}
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("SEGMENT_SEND_ALLOWLIST", "")
        get_settings.cache_clear()
        assert envios.lista_permitida() == set()  # desarrollo sin lista: no sale nada
    finally:
        get_settings.cache_clear()
