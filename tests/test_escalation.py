"""Detección por palabra clave del pedido de hablar con una persona."""

from __future__ import annotations

import pytest

from core.orchestrator.escalation import DEFAULT_ESCALATION_KEYWORDS, matches_escalation_keywords


@pytest.mark.parametrize("mensaje", [
    "quiero hablar con un asesor",
    "necesito una PERSONA REAL",
    "esto es un reclamo",
    "tengo una queja",
    "me pasas con un humano?",
])
def test_frases_que_deben_escalar(mensaje: str) -> None:
    assert matches_escalation_keywords(mensaje) is True


@pytest.mark.parametrize("mensaje", [
    "hola, cuanto cuesta el corte?",
    "gracias por todo",
    "",
])
def test_frases_que_no_deben_escalar(mensaje: str) -> None:
    assert matches_escalation_keywords(mensaje) is False


def test_es_insensible_a_mayusculas() -> None:
    assert matches_escalation_keywords("ASESOR") is True


def test_keywords_configuradas_reemplazan_a_las_por_defecto() -> None:
    assert matches_escalation_keywords("gerente", ["gerente"]) is True
    assert matches_escalation_keywords("asesor", ["gerente"]) is False


def test_lista_vacia_usa_las_por_defecto() -> None:
    """Una lista vacía es 'sin configurar', no 'nunca escalar'."""
    assert matches_escalation_keywords("asesor", []) is True


def test_las_keywords_por_defecto_no_estan_vacias() -> None:
    assert DEFAULT_ESCALATION_KEYWORDS
