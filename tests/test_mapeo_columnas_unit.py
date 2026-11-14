"""Mapeo de columnas de un CSV a la plantilla (ADR-0194 §2): heurística con sinónimos, IA acotada a
columnas libres y filas normalizadas. Sin base ni LLM: la IA se reemplaza por un doble."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pack_commerce.importacion import plantillas

from core.ia.estructurado import Consumo, ErrorFormato, ErrorProveedor, Estructurado
from core.qa import mapeo_columnas
from core.qa.importar_datos import Tabla, archivo_de_filas, leer_tabla
from core.qa.mapeo_columnas import _RespuestaIA, aplicar, mapeo_heuristico

PRODUCTOS = plantillas()[0]
TENANT = uuid.uuid4()
AGENTE = uuid.uuid4()


def _tabla(cabeceras: list[str], *filas: list[str]) -> Tabla:
    return Tabla(cabeceras=cabeceras, registros=[(i + 2, f) for i, f in enumerate(filas)])


def _consumo() -> Consumo:
    return Consumo("m", 1, 1, 1, Decimal("0"), "platform", Decimal("0.2"))


def test_la_plantilla_de_productos_se_declara_en_produccion_con_sinonimos() -> None:
    assert PRODUCTOS.en_produccion is True
    assert "codigo" in PRODUCTOS.sinonimos["sku"]


def test_heuristica_por_nombre_y_sinonimo_sin_tildes_ni_mayusculas() -> None:
    mapeo = mapeo_heuristico(["Código", "Nombre", "Valor", "Existencias", "Color"], PRODUCTOS)
    assert [(a.columna, a.origen) for a in mapeo] == [
        ("sku", "heuristica"),
        ("nombre", "heuristica"),
        ("precio", "heuristica"),
        ("stock", "heuristica"),
        (None, "ninguno"),
    ]


def test_una_columna_se_asigna_una_sola_vez_gana_la_primera() -> None:
    mapeo = mapeo_heuristico(["Ref", "SKU"], PRODUCTOS)
    assert [a.columna for a in mapeo] == ["sku", None]


def test_aplicar_normaliza_las_filas_y_tolera_registros_cortos() -> None:
    tabla = _tabla(["Código", "Producto", "Valor"], ["A1", "Cera", "28.000"], ["B2", "Aceite"])
    filas = aplicar(tabla, mapeo_heuristico(tabla.cabeceras, PRODUCTOS))
    assert filas == [
        (2, {"sku": "A1", "nombre": "Cera", "precio": "28.000"}),
        (3, {"sku": "B2", "nombre": "Aceite", "precio": ""}),
    ]


def test_leer_tabla_conserva_cabeceras_originales() -> None:
    tabla = leer_tabla("Código;Descripción larga\r\nA1;Algo\r\n\r\n".encode())
    assert tabla.cabeceras == ["Código", "Descripción larga"]
    assert tabla.registros == [(2, ["A1", "Algo"])]


def test_archivo_de_filas_ignora_lo_ajeno_y_marca_obligatorias_que_faltan() -> None:
    archivo = archivo_de_filas([(2, {"nombre": " Cera ", "color": "rojo", "precio": None})], PRODUCTOS)
    assert archivo.filas == [(2, {"nombre": "Cera", "precio": ""})]
    assert archivo.ignoradas == ["color"]
    assert archivo.faltantes == ["sku"]


def _ia(monkeypatch, salida: Any) -> list[dict[str, Any]]:
    llamadas: list[dict[str, Any]] = []

    async def generar(tenant_id: uuid.UUID, **kwargs: Any) -> Any:
        llamadas.append(kwargs)
        if isinstance(salida, Exception):
            raise salida
        return Estructurado(datos=_RespuestaIA.model_validate(salida), consumo=_consumo())

    monkeypatch.setattr("core.ia.estructurado.generar_estructurado", generar)
    return llamadas


async def test_la_ia_solo_asigna_cabeceras_pendientes_a_columnas_libres(monkeypatch) -> None:
    tabla = _tabla(["Cod. interno", "Producto", "PVP final", "Marca", "Comentario"], ["A1", "Cera", "28000", "X", "n"])
    llamadas = _ia(
        monkeypatch,
        {
            "asignaciones": [
                {"cabecera": "Cod. interno", "columna": "sku"},
                {"cabecera": "PVP final", "columna": "precio"},
                {"cabecera": "Marca", "columna": "marca"},  # no es columna de la plantilla
                {"cabecera": "Comentario", "columna": "nombre"},  # ya asignada por la heurística
                {"cabecera": "Inventada", "columna": "stock"},  # no está en el archivo
            ]
        },
    )
    mapeo, motivo = await mapeo_columnas._con_ia(
        TENANT, AGENTE, PRODUCTOS, tabla, mapeo_heuristico(tabla.cabeceras, PRODUCTOS)
    )
    assert motivo is None
    assert [(a.cabecera, a.columna, a.origen) for a in mapeo] == [
        ("Cod. interno", "sku", "ia"),
        ("Producto", "nombre", "heuristica"),
        ("PVP final", "precio", "ia"),
        ("Marca", None, "ninguno"),
        ("Comentario", None, "ninguno"),
    ]
    (llamada,) = llamadas
    assert llamada["playbook"] == "mapear_columnas"
    assert llamada["kind"] == "generacion"
    assert llamada["result"]["proposito"] == "mapeo_columnas"
    entrada = json.loads(llamada["entrada"])
    assert [c["nombre"] for c in entrada["columnas_libres"]] == ["sku", "precio", "categoria", "descripcion", "stock"]
    assert entrada["cabeceras_sin_reconocer"] == ["Cod. interno", "PVP final", "Marca", "Comentario"]
    assert entrada["muestra"] == [{"Cod. interno": "A1", "PVP final": "28000", "Marca": "X", "Comentario": "n"}]


async def test_la_ia_no_se_llama_si_no_hay_nada_que_asignar(monkeypatch) -> None:
    llamadas = _ia(monkeypatch, {"asignaciones": []})
    tabla = _tabla(["sku", "nombre", "precio", "categoria", "descripcion", "stock", "color"], ["A"] * 7)
    await mapeo_columnas._con_ia(TENANT, AGENTE, PRODUCTOS, tabla, mapeo_heuristico(tabla.cabeceras, PRODUCTOS))
    tabla = _tabla(["sku", "nombre"], ["A", "B"])
    await mapeo_columnas._con_ia(TENANT, AGENTE, PRODUCTOS, tabla, mapeo_heuristico(tabla.cabeceras, PRODUCTOS))
    assert llamadas == []


@pytest.mark.parametrize(
    ("error", "motivo"),
    [(ErrorProveedor("RuntimeError"), "proveedor"), (ErrorFormato("ValueError", _consumo()), "formato")],
)
async def test_si_la_ia_falla_quedan_sin_asignar(monkeypatch, error: Exception, motivo: str) -> None:
    _ia(monkeypatch, error)
    tabla = _tabla(["Cod. interno", "Producto"], ["A1", "Cera"])
    mapeo, recibido = await mapeo_columnas._con_ia(
        TENANT, AGENTE, PRODUCTOS, tabla, mapeo_heuristico(tabla.cabeceras, PRODUCTOS)
    )
    assert recibido == motivo
    assert [a.columna for a in mapeo] == [None, "nombre"]


async def test_no_se_llama_a_la_ia_con_cabeceras_abusivas(monkeypatch) -> None:
    llamadas = _ia(monkeypatch, {"asignaciones": []})
    muchas = _tabla([f"col{i}" for i in range(60)], ["x"] * 60)
    larga = _tabla(["x" * 500, "Producto"], ["a", "b"])
    for tabla in (muchas, larga):
        _mapeo, motivo = await mapeo_columnas._con_ia(
            TENANT, AGENTE, PRODUCTOS, tabla, mapeo_heuristico(tabla.cabeceras, PRODUCTOS)
        )
        assert motivo is None
    assert llamadas == []


async def test_cabecera_repetida_va_una_vez_a_la_ia(monkeypatch) -> None:
    llamadas = _ia(monkeypatch, {"asignaciones": [{"cabecera": "Cod", "columna": "sku"}]})
    tabla = _tabla(["Cod", "Cod", "Producto"], ["A", "B", "C"])
    mapeo, _ = await mapeo_columnas._con_ia(
        TENANT, AGENTE, PRODUCTOS, tabla, mapeo_heuristico(tabla.cabeceras, PRODUCTOS)
    )
    assert json.loads(llamadas[0]["entrada"])["cabeceras_sin_reconocer"] == ["Cod"]
    assert [a.columna for a in mapeo] == ["sku", None, "nombre"]
