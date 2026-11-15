"""Flujo de atención por agente (ADR-0195, HU-AOS-74): reglas puras, sin base.

Valida la forma del flujo, la comprobación de acciones disponibles, la semilla desde los
`flow.json` de los packs y el hash canónico que se congela con la versión.
"""

from __future__ import annotations

import pytest

from core.agents.flujo_agente import (
    FlowInvalido,
    acciones_permitidas,
    avisos_de_acciones,
    flow_hash,
    normalizar,
    semilla,
    validar_acciones,
)


class Accion:
    def __init__(self, tool: str, *, enabled: bool = False, por_superpoder: bool = False) -> None:
        self.tool = tool
        self.enabled = enabled
        self.por_superpoder = por_superpoder


def test_normaliza_y_deduplica_acciones() -> None:
    flow = normalizar(
        {
            "sections": [
                {
                    "id": "cotizacion",
                    "label": "Cotización",
                    "purpose": "Preparar el valor",
                    "tools": ["crear_pedido", "crear_pedido", "confirmar_cotizacion"],
                }
            ]
        }
    )
    assert flow == {
        "sections": [
            {
                "id": "cotizacion",
                "label": "Cotización",
                "purpose": "Preparar el valor",
                "tools": ["crear_pedido", "confirmar_cotizacion"],
            }
        ]
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"sections": [{"id": "Cotización", "label": "X", "tools": []}]},
        {"sections": [{"id": "a", "label": "X", "tools": ["con espacio"]}]},
        {"sections": [{"id": "a", "label": "X", "tools": []}, {"id": "a", "label": "Y", "tools": []}]},
        {"sections": [{"id": "a", "label": "X", "tools": [], "extra": 1}]},
        {"sections": "no-es-lista"},
    ],
)
def test_rechaza_formas_invalidas(raw: dict) -> None:
    with pytest.raises(FlowInvalido):
        normalizar(raw)


def test_valida_acciones_y_avisa_las_que_faltan() -> None:
    flow = {"sections": [{"id": "a", "label": "A", "purpose": "", "tools": ["crear_pedido", "borrar_todo"]}]}
    issues = validar_acciones(flow, {"crear_pedido"})
    assert [i.path for i in issues] == ["sections[0].tools[1]"]
    assert issues[0].code == "accion_no_disponible"
    assert avisos_de_acciones(flow, {"crear_pedido"}) == ["borrar_todo"]


def test_acciones_permitidas_solo_enciende_o_concede() -> None:
    acciones = [Accion("x", enabled=True), Accion("y"), Accion("z", por_superpoder=True)]
    assert acciones_permitidas(acciones) == {"x", "z"}


def test_semilla_toma_los_packs_y_filtra_acciones() -> None:
    seed = semilla({"pack_commerce"}, {"consultar_inventario", "generar_cotizacion", "confirmar_cotizacion"})
    ids = [s["id"] for s in seed["sections"]]
    assert "descubrimiento" in ids
    assert "cotizacion" in ids
    # El core no se guarda por agente: saludo y escalamiento no aparecen.
    assert "saludo" not in ids
    assert "escalamiento" not in ids
    tools = {t for s in seed["sections"] for t in s["tools"]}
    assert "crear_pedido" not in tools  # no la permitimos en este caso
    assert "generar_cotizacion" in tools


def test_hash_es_estable_por_claves_pero_no_por_orden_de_secciones() -> None:
    a = {"sections": [{"id": "a", "label": "A", "purpose": "", "tools": ["x", "y"]}]}
    b = {"sections": [{"purpose": "", "tools": ["x", "y"], "label": "A", "id": "a"}]}
    assert flow_hash(a) == flow_hash(b)
    c = {"sections": [{"id": "b", "label": "B", "purpose": "", "tools": []}]}
    assert flow_hash(a) != flow_hash(c)
    assert flow_hash(None) is None
