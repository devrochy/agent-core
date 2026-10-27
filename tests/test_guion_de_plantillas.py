"""HU-AOS-48 (ADR-0171): el guion de preguntas de las plantillas de agente. Lógica pura, sin red."""

from __future__ import annotations

import pytest

from core.presets import PlantillaAgente, load_plantilla
from core.presets.guion import RespuestasInvalidas, resolver

VENTAS = ["pack_commerce.tomar_pedido", "pack_commerce.contratar_plan"]


def test_las_cuatro_plantillas_traen_un_guion_corto_y_valido() -> None:
    for key in ("ventas", "agenda", "cobranza", "suscripciones"):
        plantilla = load_plantilla(key)
        assert 1 <= len(plantilla.preguntas) <= 8, key
        # El asistente suma identidad, conexión, verificación y publicación: queda margen para ≤ 10 pasos.
        assert len(plantilla.preguntas) + 4 <= 10, key


def test_ventas_pregunta_el_tipo_de_negocio_y_deja_el_superpoder_que_corresponde() -> None:
    ventas = load_plantilla("ventas")
    assert resolver(ventas, {"que_vendes": "planes"}).procedimientos == ["pack_commerce.contratar_plan"]
    assert resolver(ventas, {"que_vendes": "productos"}).procedimientos == ["pack_commerce.tomar_pedido"]
    assert resolver(ventas, {"que_vendes": "ambos"}).procedimientos == VENTAS
    # Sin responder, se aplica el defecto del guion (productos).
    assert resolver(ventas, {}).procedimientos == ["pack_commerce.tomar_pedido"]


def test_una_plantilla_sin_pregunta_de_superpoderes_los_conserva_todos() -> None:
    agenda = load_plantilla("agenda")
    assert resolver(agenda, {}).procedimientos == agenda.procedimientos


def test_el_largo_de_respuesta_solo_cambia_si_la_opcion_lo_declara() -> None:
    ventas = load_plantilla("ventas")
    assert resolver(ventas, {"largo_respuesta": "cortas"}).largo_maximo == 300
    assert resolver(ventas, {"largo_respuesta": "detalladas"}).largo_maximo == 900
    assert resolver(ventas, {"largo_respuesta": "normal"}).largo_maximo is None
    assert resolver(ventas, {}).largo_maximo is None


def test_respuesta_fuera_de_las_opciones_o_pregunta_inexistente_da_un_error_por_pregunta() -> None:
    ventas = load_plantilla("ventas")
    with pytest.raises(RespuestasInvalidas) as exc:
        resolver(ventas, {"que_vendes": "servicios", "largo_respuesta": "cortas", "nada": "x"})
    assert set(exc.value.por_pregunta) == {"que_vendes", "nada"}


def _con_texto() -> PlantillaAgente:
    base = load_plantilla("cobranza").model_dump()
    base["preguntas"] = [{"id": "nombre", "texto": "¿Cómo se llama?", "tipo": "texto"}]
    return PlantillaAgente.model_validate(base)


def test_pregunta_de_texto_es_el_nombre_y_no_admite_vacio() -> None:
    plantilla = _con_texto()
    assert resolver(plantilla, {"nombre": "  Cobros Ana "}).nombre == "Cobros Ana"
    assert resolver(plantilla, {}).nombre is None
    for malo in ("   ", "x" * 121):
        with pytest.raises(RespuestasInvalidas):
            resolver(plantilla, {"nombre": malo})


def test_la_plantilla_rechaza_un_guion_incoherente() -> None:
    base = load_plantilla("ventas").model_dump()
    opciones = [{"id": "a", "texto": "A"}, {"id": "b", "texto": "B"}]

    def _con(preguntas: list[dict]) -> None:
        PlantillaAgente.model_validate({**base, "preguntas": preguntas})

    with pytest.raises(ValueError, match="defecto"):
        _con([{"id": "q", "texto": "¿?", "opciones": opciones, "defecto": "z"}])
    with pytest.raises(ValueError, match="dos opciones"):
        _con([{"id": "q", "texto": "¿?", "opciones": opciones[:1], "defecto": "a"}])
    with pytest.raises(ValueError, match="id repetido"):
        q = {"id": "q", "texto": "¿?", "opciones": opciones, "defecto": "a"}
        _con([q, q])
    with pytest.raises(ValueError, match="procedimientos que la plantilla no tiene"):
        raro = [{"id": "a", "texto": "A", "procedimientos": ["pack_x.nada"]}, opciones[1]]
        _con([{"id": "q", "texto": "¿?", "opciones": raro, "defecto": "a"}])
    with pytest.raises(ValueError, match="texto"):
        _con([{"id": "n", "texto": "¿?", "tipo": "texto", "opciones": opciones}])
