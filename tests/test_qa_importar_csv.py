"""HU-QA-12 · Importar datos existentes por agente: lectura y validación del CSV (ADR-0180).

Sin base de datos: el archivo es dato no confiable y estas pruebas fijan cómo se lee y se valida.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pack_calendar.importacion import plantillas as plantillas_agenda
from pack_cobranza.importacion import plantillas as plantillas_cobranza
from pack_commerce.importacion import plantillas as plantillas_comercio

from core.qa.contrato_importacion import (
    TOPE_BYTES,
    TOPE_FILAS,
    entero,
    fecha,
    normalizar_cabecera,
    numero,
)
from core.qa.datos_generados import ErrorDatosDePrueba
from core.qa.importar_datos import leer_archivo, plantilla_csv, validar

PRODUCTOS = next(p for p in plantillas_comercio() if p.id == "productos")
TIPOS = plantillas_agenda()[0]
CARTERA = plantillas_cobranza()[0]


def test_normaliza_cabeceras() -> None:
    assert normalizar_cabecera(" Número de Documento ") == "numero_de_documento"
    assert normalizar_cabecera("Descripción") == "descripcion"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("1500", "1500"),
        ("1.500.000", "1500000"),
        ("1,500,000", "1500000"),
        ("1,500.50", "1500.50"),
        ("1.500,50", "1500.50"),
        ("$ 20.000", "20000"),
        ("45000,5", "45000.5"),
        ("12.5", "12.5"),
    ],
)
def test_numero_como_en_excel(texto: str, esperado: str) -> None:
    assert numero(texto) == Decimal(esperado)


@pytest.mark.parametrize("texto", ["", "abc", "12x", "1.2.3,4,5a", "NaN", "sNaN", "Infinity", "-Infinity", "1E+900000000", "1e-99"])
def test_numero_invalido(texto: str) -> None:
    with pytest.raises(ValueError):
        numero(texto)


def test_valores_no_finitos_son_error_de_la_fila_y_no_una_excepcion() -> None:
    contenido = b"sku,nombre,precio\r\nA1,Cera,NaN\r\nA2,Aceite,Infinity\r\nA3,Gel,1E+900000000\r\n"
    filas = validar(leer_archivo(contenido, PRODUCTOS), PRODUCTOS)
    assert [f.errores[0][0] for f in filas] == ["precio", "precio", "precio"]
    with pytest.raises(ValueError):
        entero("Infinity")


def test_entero_y_fecha() -> None:
    assert entero("30") == 30
    with pytest.raises(ValueError):
        entero("30,5")
    assert fecha("2026-10-28").isoformat() == "2026-10-28"
    assert fecha("28/10/2026").isoformat() == "2026-10-28"
    with pytest.raises(ValueError):
        fecha("ayer")


def test_plantilla_descargable_trae_cabeceras_y_ejemplos_validos() -> None:
    for plantilla in (PRODUCTOS, TIPOS, CARTERA):
        contenido = plantilla_csv(plantilla)
        assert contenido.startswith(b"\xef\xbb\xbf")  # BOM: Excel respeta las tildes
        archivo = leer_archivo(contenido, plantilla)
        assert archivo.ignoradas == [] and archivo.faltantes == []
        filas = validar(archivo, plantilla)
        assert filas and all(f.datos is not None for f in filas), [f.errores for f in filas]


def test_acepta_punto_y_coma_bom_y_windows_1252() -> None:
    csv = "Nombre;Duración_minutos;Descripción\r\nCorte;30;Clásico\r\nBarba;20;\r\n"
    for contenido in (csv.encode("utf-8-sig"), csv.encode("cp1252")):
        archivo = leer_archivo(contenido, TIPOS)
        assert archivo.faltantes == []
        filas = validar(archivo, TIPOS)
        assert [f.datos.nombre for f in filas if f.datos] == ["Corte", "Barba"]
        assert filas[0].datos.descripcion == "Clásico"  # type: ignore[union-attr]


def test_columnas_desconocidas_se_ignoran_y_telefono_nunca_llega_a_la_fila() -> None:
    contenido = (
        b"cartera,numero_documento,nombre,telefono,correo\r\n"
        b"Cartera A,1000000001,Juana,3001234567,j@correo.test\r\n"
    )
    archivo = leer_archivo(contenido, CARTERA)
    assert archivo.ignoradas == ["telefono", "correo"]
    (fila,) = archivo.filas
    assert set(fila[1]) == {"cartera", "numero_documento", "nombre"}
    datos = validar(archivo, CARTERA)[0].datos
    assert datos is not None and not hasattr(datos, "telefono")


def test_columnas_obligatorias_faltantes() -> None:
    archivo = leer_archivo(b"nombre,descripcion\r\nCorte,x\r\n", TIPOS)
    assert archivo.faltantes == ["duracion_minutos"]


def test_errores_por_fila_en_espanol() -> None:
    contenido = (
        b"nombre,duracion_minutos,descripcion\r\n"
        b"Corte,30,\r\n"
        b"X,30,\r\n"
        b"Barba,abc,\r\n"
        b"Cejas,600,\r\n"
        b",20,\r\n"
    )
    filas = validar(leer_archivo(contenido, TIPOS), TIPOS)
    assert filas[0].errores == []
    assert [(f.numero, f.errores[0][0]) for f in filas[1:]] == [
        (3, "nombre"),
        (4, "duracion_minutos"),
        (5, "duracion_minutos"),
        (6, "nombre"),
    ]
    assert filas[2].errores[0][1] == "Debe ser un número."
    assert filas[1].errores[0][1] == "Es muy corto o está vacío."


def test_clave_repetida_en_el_archivo_es_error_de_la_segunda() -> None:
    contenido = b"nombre,duracion_minutos\r\nCorte,30\r\nCORTE,45\r\n"
    filas = validar(leer_archivo(contenido, TIPOS), TIPOS)
    assert filas[0].datos is not None
    assert filas[1].datos is None and "fila 2" in filas[1].errores[0][1]


def test_prestamo_incompleto_o_incoherente() -> None:
    cabecera = "cartera,numero_documento,nombre,valor,num_cuotas,interes_porcentaje,fecha_desembolso,cuotas_pagadas\r\n"
    contenido = (
        cabecera
        + "Cartera A,1000001,Ana,500000,10,20,2026-10-01,3\r\n"
        + "Cartera A,1000002,Beto,500000,,20,2026-10-01,0\r\n"
        + "Cartera A,1000003,Carla,500000,10,20,2026-10-01,11\r\n"
        + "Cartera A,1000004,Dani,500000,10,20,2999-01-01,0\r\n"
        + "Cartera A,1000005,Eva,,,,,2\r\n"
        + "Cartera A,abc,Fer,,,,,\r\n"
    ).encode()
    filas = validar(leer_archivo(contenido, CARTERA), CARTERA)
    assert filas[0].errores == []
    assert "num_cuotas" in filas[1].errores[0][1]
    assert "cuotas pagadas" in filas[2].errores[0][1].lower()
    assert filas[3].errores[0][0] == "fecha_desembolso"
    assert "préstamo" in filas[4].errores[0][1]
    assert filas[5].errores[0][0] == "numero_documento"


def test_comercio_ya_no_importa_planes_solo_productos() -> None:
    """HU-COM-17: los planes son de Suscripciones; importarlos por CSV queda para una HU aparte."""
    assert [p.id for p in plantillas_comercio()] == ["productos"]


def test_validacion_de_productos() -> None:
    productos = validar(
        leer_archivo(b"sku,nombre,precio,categoria\r\nA1,Cera,-5,\r\nA2,Aceite,1000,Barba > Aceites\r\nA3,Gel,10,\r\n", PRODUCTOS),
        PRODUCTOS,
    )
    assert "negativo" in productos[0].errores[0][1]
    assert productos[1].datos.ruta_de_categoria() == ["Barba", "Aceites"]  # type: ignore[union-attr]
    assert productos[2].datos.ruta_de_categoria() == ["Sin categoría"]  # type: ignore[union-attr]


def test_topes_de_tamano_y_de_filas() -> None:
    with pytest.raises(ErrorDatosDePrueba) as exc:
        leer_archivo(b"x" * (TOPE_BYTES + 1), TIPOS)
    assert exc.value.status == 413
    filas = "".join(f"Tipo {i},30\r\n" for i in range(TOPE_FILAS + 1))
    with pytest.raises(ErrorDatosDePrueba) as exc:
        leer_archivo(("nombre,duracion_minutos\r\n" + filas).encode(), TIPOS)
    assert exc.value.code == "demasiadas_filas"
    for vacio in (b"", b"\r\n\r\n", b"nombre,duracion_minutos\r\n"):
        with pytest.raises(ErrorDatosDePrueba) as exc:
            leer_archivo(vacio, TIPOS)
        assert exc.value.code == "archivo_vacio"
