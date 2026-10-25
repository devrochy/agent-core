"""`pasar_a_agente` en la oferta de comandos (HU-AOS-19, adenda de ADR-0116). Puro."""

from __future__ import annotations

from core.kernel.procedures import comandos


def test_se_ofrece_solo_con_destinos_y_al_final() -> None:
    assert comandos.aplicables(foco=None, candidatos=[]) == []
    assert comandos.aplicables(foco=None, candidatos=[], destinos_de_handoff=["soporte"]) == [comandos.PASAR]


def test_el_schema_es_un_enum_de_keys_y_los_textos_existen() -> None:
    (tool,) = comandos.herramientas([comandos.PASAR], foco=None, candidatos=[], destinos_de_handoff=["soporte", "agenda"])
    assert tool.name == comandos.PASAR and tool.parameters["required"] == ["agente"]
    assert tool.parameters["properties"]["agente"]["enum"] == ["agenda", "soporte"]
    assert tool.parameters["additionalProperties"] is False
    assert '"agenda"' in tool.description and '"soporte"' in tool.description
    for clave in ("pasar_aceptado", "pasar_negado"):
        assert comandos.texto(clave, agente='"x"')


def test_argumentos_del_comando() -> None:
    assert comandos.argumentos_invalidos(comandos.PASAR, {"agente": "soporte"}) is None
    assert comandos.argumentos_invalidos(comandos.PASAR, {"agente": 3}) is not None
    assert comandos.argumentos_invalidos(comandos.PASAR, {"agente": "x", "otro": 1}) is not None
