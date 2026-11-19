"""SPEC-40 · mapeo puro de la definición a interactivo, plantilla de Meta y texto de respaldo."""

from __future__ import annotations

import copy
import uuid

import pytest

from core.gateway.meta_templates import TemplateValidationError, validate_template_components
from core.kernel.opciones import mapeo, payload
from core.kernel.opciones.definicion import Definicion
from core.kernel.procedures.linter import PREFIJOS_OPCIONES

TENANT, CONV, SEND = uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)


def definicion(n: int) -> Definicion:
    opcion = {
        "accion": {"tool": "consultar_estado_pedido", "args_fijos": {}, "args_contexto": {}, "confirmar": False},
        "respuesta_ok": "Listo.",
        "respuesta_error": "No pude.",
        "result_keys": [],
    }
    return Definicion.model_validate(
        {
            "body": "Hola {cliente.nombre}, tu pedido {pedido.numero} está listo.",
            "footer": "Gracias por elegirnos",
            "variables": ["cliente.nombre", "pedido.numero"],
            "opciones": [{**copy.deepcopy(opcion), "id": f"o{i}", "titulo": f"Opción {i}"} for i in range(1, n + 1)],
        }
    )


def payloads(d: Definicion) -> dict[str, str]:
    return {o.id: payload.construir("s", "opt", TENANT, CONV, SEND, o.id) for o in d.opciones}


VALORES = {"cliente.nombre": "Ana\nPérez", "pedido.numero": "A-1"}


def test_hasta_tres_opciones_son_botones() -> None:
    d = definicion(3)
    cuerpo = mapeo.renderizar(d.body, VALORES)
    interactivo = mapeo.a_interactivo(d, cuerpo, payloads(d))
    assert interactivo == {
        "type": "button",
        "body": "Hola Ana Pérez, tu pedido A-1 está listo.",
        "buttons": [{"id": payloads(d)[f"o{i}"], "title": f"Opción {i}"} for i in (1, 2, 3)],
        "footer": "Gracias por elegirnos",
    }
    assert all(len(b["id"]) <= payload.MAX_PAYLOAD for b in interactivo["buttons"])


def test_de_cuatro_a_diez_opciones_son_una_lista() -> None:
    for n in (4, 10):
        d = definicion(n)
        interactivo = mapeo.a_interactivo(d, "Hola", payloads(d))
        assert interactivo["type"] == "list" and interactivo["button"] == "Ver opciones"
        assert [r["title"] for r in interactivo["rows"]] == [f"Opción {i}" for i in range(1, n + 1)]
        assert len({r["id"] for r in interactivo["rows"]}) == n
    assert "footer" not in mapeo.a_interactivo(definicion(2).model_copy(update={"footer": None}), "Hola", payloads(definicion(2)))


def test_el_interactivo_cumple_los_topes_del_sender() -> None:
    from core.gateway import sender

    d = definicion(3)
    interactivo = mapeo.a_interactivo(d, "Hola", payloads(d))
    # `interactivo_valido` de develop solo conoce botones; las listas las valida el PR de listas (#590).
    assert sender.interactivo_valido({k: v for k, v in interactivo.items() if k != "footer"})


def test_falta_un_payload_no_se_envia_un_boton_sin_el() -> None:
    d = definicion(2)
    with pytest.raises(KeyError):
        mapeo.a_interactivo(d, "Hola", {"o1": "opt:x"})


def test_golden_de_los_componentes_de_meta() -> None:
    assert mapeo.componentes_meta(definicion(2), {"cliente.nombre": "Ana", "pedido.numero": "A-1"}) == [
        {
            "type": "BODY",
            "text": "Hola {{cliente_nombre}}, tu pedido {{pedido_numero}} está listo.",
            "example": {
                "body_text_named_params": [
                    {"param_name": "cliente_nombre", "example": "Ana"},
                    {"param_name": "pedido_numero", "example": "A-1"},
                ]
            },
        },
        {"type": "FOOTER", "text": "Gracias por elegirnos"},
        {
            "type": "BUTTONS",
            "buttons": [{"type": "QUICK_REPLY", "text": "Opción 1"}, {"type": "QUICK_REPLY", "text": "Opción 2"}],
        },
    ]


@pytest.mark.parametrize("n", [1, 3, 10])
def test_los_componentes_pasan_la_validacion_de_meta_del_repo(n: int) -> None:
    validate_template_components("UTILITY", mapeo.componentes_meta(definicion(n)), "NAMED")


def test_la_validacion_de_meta_rechaza_lo_que_el_mapeo_no_arma() -> None:
    comps = mapeo.componentes_meta(definicion(1))
    comps[0].pop("example")
    with pytest.raises(TemplateValidationError):
        validate_template_components("UTILITY", comps, "NAMED")


def test_sin_pie_ni_variables_no_hay_footer_ni_example() -> None:
    d = Definicion.model_validate({**definicion(1).model_dump(), "body": "Hola", "footer": None, "variables": []})
    comps = mapeo.componentes_meta(d)
    assert [c["type"] for c in comps] == ["BODY", "BUTTONS"] and "example" not in comps[0]


def test_render_y_parametros_reusan_event_notices() -> None:
    d = definicion(2)
    assert mapeo.cuerpo_de_plantilla_renderizado(d, VALORES) == "Hola Ana Pérez, tu pedido A-1 está listo."
    assert mapeo.parametros_de_cuerpo_meta(d, VALORES) == [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": "Ana Pérez", "parameter_name": "cliente_nombre"},
                {"type": "text", "text": "A-1", "parameter_name": "pedido_numero"},
            ],
        }
    ]


def test_texto_de_respaldo_lista_las_opciones_sin_ejecutar_nada() -> None:
    texto = mapeo.texto_de_respaldo(definicion(3), "Hola Ana")
    assert texto == "Hola Ana\n\n1. Opción 1\n2. Opción 2\n3. Opción 3"
    assert not any(texto.startswith(p) or p in texto for p in PREFIJOS_OPCIONES)


def test_un_valor_sin_dato_deja_la_variable_visible_y_no_revienta() -> None:
    assert mapeo.renderizar("Hola {cliente.nombre}", {}) == "Hola {cliente.nombre}"
