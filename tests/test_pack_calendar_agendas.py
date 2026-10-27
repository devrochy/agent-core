"""Agendas del negocio (HU-CAL-08): funciones puras de `pack_calendar.agendas`."""

from __future__ import annotations

import uuid

import pytest
from pack_calendar.agendas import (
    Agenda,
    color_siguiente,
    config_de,
    disponible,
    limpiar_aliases,
    limpiar_nombre,
    multi_activo,
    nombre_desde_email,
    normalizar,
    por_id,
    resolver,
)
from pack_calendar.config import CalendarConfig


def _ag(nombre: str, **kw) -> Agenda:
    return Agenda(id=uuid.uuid4(), name=nombre, **kw)


def _cfg(**kw) -> CalendarConfig:
    return CalendarConfig(source=kw.pop("source", "interno"), **kw)


def test_normalizar_quita_acentos_y_mayusculas() -> None:
    assert normalizar("  Dra.  Pérez ") == "dra. perez"


def test_limpiar_nombre_una_linea_y_tope() -> None:
    assert limpiar_nombre("Dra.\nAna\t\x00 Pérez") == "Dra. Ana Pérez"
    assert len(limpiar_nombre("x" * 200)) == 60


def test_limpiar_aliases_sin_repetidos() -> None:
    assert limpiar_aliases(["Sede Norte", "sede  norte", "Doctora", ""]) == ["Sede Norte", "Doctora"]


def test_nombre_desde_email_determinista_y_sin_colision() -> None:
    assert nombre_desde_email("ana.perez@x.com", set()) == "Ana Perez"
    assert nombre_desde_email("ana.perez@x.com", {"ana perez"}) == "Ana Perez 2"
    assert nombre_desde_email("ana.perez@x.com", {"Ana Perez", "Ana Perez 2"}) == "Ana Perez 3"
    assert nombre_desde_email("", set()) == "Agenda de Google"


def test_color_siguiente_rota_la_paleta() -> None:
    assert color_siguiente([]) == "cat-1"
    assert color_siguiente(["cat-1"]) == "cat-2"
    assert color_siguiente(["cat-1", "cat-2", "cat-3", "cat-4"]) == "cat-1"


def test_resolver_por_id_nombre_alias_y_parcial() -> None:
    ana = _ag("Dra. Ana Pérez", aliases=("doctora", "sede norte"))
    luis = _ag("Dr. Luis Gómez", aliases=("doctor",))
    todas = [ana, luis]
    assert resolver(str(luis.id), todas) == (luis, [])
    assert resolver("dr. luis gomez", todas) == (luis, [])
    assert resolver("Sede Norte", todas) == (ana, [])
    assert resolver("luis", todas) == (luis, [])
    assert resolver("nadie", todas) == (None, [])
    assert resolver("", todas) == (None, [])


def test_resolver_ambiguo_devuelve_candidatos() -> None:
    a = _ag("Dra. Ana", aliases=("doctora",))
    b = _ag("Dra. Eva", aliases=("doctora",))
    agenda, candidatos = resolver("doctora", [a, b])
    assert agenda is None and candidatos == [a, b]


def test_por_id_none_es_la_principal() -> None:
    principal = _ag("Principal", is_default=True)
    otra = _ag("Otra")
    assert por_id(None, [otra, principal]) is principal
    assert por_id(otra.id, [otra, principal]) is otra
    assert por_id(uuid.uuid4(), [otra, principal]) is None


def test_multi_activo_exige_bandera_y_dos_agendas_disponibles() -> None:
    uno = [_ag("A", is_default=True)]
    dos = [_ag("A", is_default=True), _ag("B")]
    assert not multi_activo(_cfg(), uno)
    assert multi_activo(_cfg(), dos)
    assert not multi_activo(_cfg(multi_agenda_modo="desactivado"), dos)


def test_multi_activo_ignora_agendas_de_google_sin_conexion_o_inactivas() -> None:
    gcaida = _ag("G", connection_id=uuid.uuid4(), conexion_activa=False)
    gok = _ag("H", connection_id=uuid.uuid4(), conexion_activa=True)
    inactiva = _ag("I", active=False)
    interna = _ag("A", is_default=True)
    assert not multi_activo(_cfg(), [interna, gcaida])
    assert not multi_activo(_cfg(), [interna, inactiva])
    assert multi_activo(_cfg(), [interna, gok])


def test_agenda_principal_sin_vincular_en_modo_google_no_esta_disponible() -> None:
    cfg = _cfg(source="google", client_id="x", client_secret="y")
    sin_vincular = _ag("Principal", is_default=True)
    assert not disponible(sin_vincular, cfg)
    assert disponible(_ag("Interna"), cfg)


def test_config_de_hereda_y_pisa() -> None:
    cfg = _cfg(timezone="America/Bogota", calendar_id="primary")
    heredada = _ag("A")
    assert config_de(heredada, cfg) == cfg
    propia = _ag(
        "B", timezone="America/Mexico_City", business_hours='{"mon": "08:00-12:00"}',
        connection_id=uuid.uuid4(), google_calendar_id="abc@group.calendar.google.com",
    )
    efectiva = config_de(propia, cfg)
    assert efectiva.timezone == "America/Mexico_City"
    assert efectiva.business_hours == '{"mon": "08:00-12:00"}'
    assert efectiva.calendar_id == "abc@group.calendar.google.com"
    # `primary` (valor por defecto de la agenda) deja mandar al calendario de la config
    assert config_de(_ag("C", connection_id=uuid.uuid4()), _cfg(calendar_id="x@y.com")).calendar_id == "x@y.com"
    assert efectiva.source == "google"
    assert cfg.timezone == "America/Bogota"  # la global no se toca


def test_config_de_agenda_interna_en_negocio_google_no_usa_google() -> None:
    cfg = _cfg(source="google", client_id="x", client_secret="y")
    assert config_de(_ag("Interna"), cfg).source == "interno"


def test_bandera_invalida_se_rechaza() -> None:
    with pytest.raises(ValueError):
        CalendarConfig(multi_agenda_modo="siempre")
