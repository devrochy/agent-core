"""Importación de leads desde CSV: parseo puro, sin base de datos ni red."""

from __future__ import annotations

from decimal import Decimal

import pytest

from core.leads.importar import CsvInvalido, leer_csv


def test_lee_un_csv_con_bom_punto_y_coma_y_encabezados_en_espanol() -> None:
    contenido = (
        "\ufeffnombre;teléfono;correo;interés;estado;valor;seguimiento;nota_seguimiento\n"
        "Ana Pérez;+57 300 123 4567;ANA@CORREO.COM;Plan mensual;Calificado;250.000;2026-12-01T09:00;Llamar\n"
    ).encode()

    filas, errores = leer_csv(contenido)

    assert errores == []
    assert len(filas) == 1
    fila = filas[0]
    assert fila.nombre == "Ana Pérez"
    assert fila.telefono == "+573001234567"
    assert fila.email == "ana@correo.com"
    assert fila.interes == "Plan mensual"
    assert fila.estado == "calificado"
    assert fila.valor == Decimal("250000")
    assert fila.seguimiento_en == "2026-12-01T09:00"
    assert fila.seguimiento_nota == "Llamar"


def test_una_fila_mala_no_tumba_las_buenas_y_dice_su_numero_de_linea() -> None:
    contenido = (
        b"nombre,telefono,email\n"
        b"Ana,3001234567,\n"
        b"Sin contacto,,\n"
        b"Beto,,beto@correo.com\n"
    )

    filas, errores = leer_csv(contenido)

    assert [f.nombre for f in filas] == ["Ana", "Beto"]
    assert len(errores) == 1
    assert errores[0].fila == 3
    assert "teléfono o el correo" in errores[0].mensaje


def test_acepta_encabezados_en_ingles_y_un_solo_correo() -> None:
    filas, errores = leer_csv(b"name,email,interest\nAna,ana@correo.com,Plan\n")

    assert errores == []
    assert filas[0].nombre == "Ana"
    assert filas[0].telefono is None
    assert filas[0].email == "ana@correo.com"


def test_un_lead_perdido_exige_motivo() -> None:
    filas, errores = leer_csv(b"nombre,telefono,estado\nAna,3001234567,perdido\n")

    assert filas == []
    assert "motivo" in errores[0].mensaje


def test_rechaza_archivos_sin_nombre_o_sin_contacto() -> None:
    with pytest.raises(CsvInvalido, match="nombre"):
        leer_csv(b"telefono,email\n3001234567,ana@correo.com\n")
    with pytest.raises(CsvInvalido, match="telefono"):
        leer_csv(b"nombre,interes\nAna,Plan\n")


def test_un_archivo_vacio_no_se_acepta() -> None:
    with pytest.raises(CsvInvalido, match="vacío"):
        leer_csv(b"")
