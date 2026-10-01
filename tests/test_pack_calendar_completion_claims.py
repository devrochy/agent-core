"""`completion_claims` parametrizables en pack-calendar (ADR-0025, #157)."""

from __future__ import annotations

import pytest
from pack_calendar.module import (
    _COMPLETION_CLAIMS,
    _parse_custom_completion_claims,
)


def test_parse_claim_custom():
    claims = _parse_custom_completion_claims("dato_tomado: tomé tu dato | quedó registrado => crear_lead")
    assert "dato_tomado" in claims
    pattern, tools = claims["dato_tomado"]
    assert tools == {"crear_lead"}
    assert pattern.search("Quedó registrado, te confirmo en un momento")
    assert not pattern.search("registramos todo")


def test_parse_frases_escapadas_no_regex():
    # El punto NO debe actuar como comodín (las frases se escapan, anti-ReDoS).
    claims = _parse_custom_completion_claims("precio: cuesta 10.5 => consultar_inventario")
    pattern, _ = claims["precio"]
    assert pattern.search("cuesta 10.5")
    assert not pattern.search("cuesta 10X5")


def test_parse_ignora_lineas_malformadas():
    assert _parse_custom_completion_claims("agendé tu cita") == {}
    assert _parse_custom_completion_claims(": frase => crear_cita") == {}
    assert _parse_custom_completion_claims("etiqueta:  => crear_cita") == {}
    assert _parse_custom_completion_claims("etiqueta: frase => ") == {}
    assert _parse_custom_completion_claims("# comentario") == {}


def test_claims_incorporados_presentes():
    for label in ("agendada", "reagendada", "cancelada", "correo_enviado", "listado"):
        assert label in _COMPLETION_CLAIMS


@pytest.mark.parametrize(
    "texto,label",
    [
        ("Quedó agendada tu demo", "agendada"),
        ("Tu cita quedó reprogramada", "reagendada"),
        ("Tu cita quedó cancelada", "cancelada"),
        ("Te envié el correo de confirmación", "correo_enviado"),
        ("No tenés citas agendadas", "listado"),
    ],
)
def test_claims_incorporados_matchean(texto: str, label: str) -> None:
    pattern, _ = _COMPLETION_CLAIMS[label]
    assert pattern.search(texto)