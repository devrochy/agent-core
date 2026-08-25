"""División de la respuesta en burbujas. Nunca se descarta contenido."""

from __future__ import annotations

import pytest

from core.gateway.message_splitter import split_for_bubbles

TRES = "Uno.\n\nDos.\n\nTres."
CINCO = "\n\n".join(f"Parrafo {i}." for i in range(1, 6))


def test_texto_vacio_no_produce_burbujas() -> None:
    assert split_for_bubbles("") == []
    assert split_for_bubbles("   \n  ") == []


def test_un_mensaje_devuelve_una_sola_burbuja() -> None:
    assert split_for_bubbles(TRES, "un_mensaje") == ["Uno.\n\nDos.\n\nTres."]


def test_pocos_cortos_divide_por_parrafo() -> None:
    assert split_for_bubbles(TRES, "pocos_cortos") == ["Uno.", "Dos.", "Tres."]


@pytest.mark.parametrize(("estilo", "maximo"), [("un_mensaje", 1), ("pocos_cortos", 3), ("varios_cortos", 6)])
def test_se_respeta_el_maximo_de_cada_estilo(estilo: str, maximo: int) -> None:
    texto = "\n\n".join(f"Parrafo {i}." for i in range(1, 12))
    assert len(split_for_bubbles(texto, estilo)) <= maximo


def test_el_exceso_se_fusiona_en_la_ultima_burbuja() -> None:
    burbujas = split_for_bubbles(CINCO, "pocos_cortos")
    assert len(burbujas) == 3
    assert "Parrafo 3." in burbujas[-1]
    assert "Parrafo 5." in burbujas[-1]


def test_no_se_pierde_contenido_al_recortar() -> None:
    burbujas = split_for_bubbles(CINCO, "pocos_cortos")
    unido = " ".join(burbujas)
    for i in range(1, 6):
        assert f"Parrafo {i}." in unido


def test_varios_cortos_divide_tambien_por_oracion() -> None:
    burbujas = split_for_bubbles("Hola. Que tal. Adios.", "varios_cortos")
    assert burbujas == ["Hola.", "Que tal.", "Adios."]


def test_estilo_desconocido_cae_al_maximo_por_defecto() -> None:
    assert len(split_for_bubbles(CINCO, "estilo_inventado")) == 3
