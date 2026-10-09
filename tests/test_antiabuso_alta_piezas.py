"""Piezas puras del antiabuso del alta (HU-SH-07, ADR-0125): normalización, huellas,
dominios desechables y validación de los documentos legales. Lo que necesita la BD está en
`tests/integration/test_antiabuso_alta.py`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from core.provisioning import legal
from core.provisioning.antiabuso import (
    SENAL_DOCUMENTO,
    SENAL_TELEFONO,
    _huellas_y_senales,
    dominio_desechable,
    huella,
    normalizar_documento,
    normalizar_telefono,
)


@pytest.mark.parametrize("valor", ["+57 300 123 4567", "573001234567", "3001234567", "(300) 123-4567"])
def test_un_mismo_celular_en_cualquier_formato_da_lo_mismo(valor: str) -> None:
    assert normalizar_telefono(valor) == "3001234567"


@pytest.mark.parametrize("valor", ["", "123", "1" * 16, None])
def test_lo_que_no_es_un_telefono_no_da_huella(valor: str | None) -> None:
    assert normalizar_telefono(valor) is None


@pytest.mark.parametrize("valor", ["1.020.304.050", "1020304050", " 1020304050 "])
def test_una_cedula_en_cualquier_formato_da_lo_mismo(valor: str) -> None:
    assert normalizar_documento(valor) == "1020304050"


def test_el_nit_con_y_sin_digito_de_verificacion_es_el_mismo() -> None:
    assert normalizar_documento("900.123.456-7") == normalizar_documento("900123456") == "900123456"


@pytest.mark.parametrize("valor", ["1", "1111111", "x" * 20, ""])
def test_un_documento_que_no_sirve_no_da_huella(valor: str) -> None:
    assert normalizar_documento(valor) is None


def test_la_huella_no_es_el_dato_ni_un_sha256_a_secas(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    from core.config import get_settings

    monkeypatch.setenv("APP_SECRET_KEY", "clave-uno")
    get_settings.cache_clear()
    una = huella("tel", "3001234567")
    assert una.startswith("tel:")
    assert "3001234567" not in una
    assert una[4:] != hashlib.sha256(b"tel:3001234567").hexdigest()
    # Mismo dato, otro tipo: otra huella.
    assert huella("doc", "3001234567") != una
    # Depende de la clave del servidor: sin ella no se puede reconstruir probando números.
    monkeypatch.setenv("APP_SECRET_KEY", "clave-dos")
    get_settings.cache_clear()
    assert huella("tel", "3001234567") != una
    get_settings.cache_clear()


def test_huellas_y_senales_del_checkout() -> None:
    huellas, senales = _huellas_y_senales("+573001234567", "300 123 4567", "1.020.304.050")
    assert len(huellas) == 2  # el WhatsApp y el teléfono del checkout son el mismo número
    assert senales == []
    _h, senales = _huellas_y_senales(None, "12", "1")
    assert senales == [SENAL_TELEFONO, SENAL_DOCUMENTO]
    huellas, senales = _huellas_y_senales(None, None, None)
    assert (huellas, senales) == ([], [])


@pytest.mark.parametrize(
    ("correo", "esperado"),
    [
        ("ana@mailinator.com", True),
        ("ana@sub.mailinator.com", True),
        ("ana@YOPMAIL.com", True),
        ("ana@gmail.com", False),
        ("ana@mailinator.com.co", False),
        ("sin-arroba", False),
    ],
)
def test_dominio_desechable(correo: str, esperado: bool) -> None:
    assert dominio_desechable(correo) is esperado


@pytest.mark.parametrize(
    "url",
    ["http://solvershub.co/terminos", "https://", "https://localhost/x", "https://a b.co", "https://u:p@x.co/t", "javascript:alert(1)"],
)
def test_un_enlace_legal_que_no_es_https_publico_se_rechaza(url: str) -> None:
    with pytest.raises(legal.DocumentoInvalido):
        legal.url_valida(url)


@pytest.mark.parametrize("version", ["", "-v1", "v 1", "v1/2", "ñ", "x" * 41])
def test_una_version_rara_se_rechaza(version: str) -> None:
    with pytest.raises(legal.DocumentoInvalido):
        legal.version_valida(version)


def test_la_version_legal_cambia_con_cualquier_documento() -> None:
    ahora = datetime.now(UTC)

    def juego(dpa: str) -> dict[str, legal.Documento]:
        return {
            "terminos": legal.Documento("terminos", "v1", "https://s.co/t", ahora),
            "privacidad": legal.Documento("privacidad", "v1", "https://s.co/p", ahora),
            "dpa": legal.Documento("dpa", dpa, "https://s.co/d", ahora),
        }

    assert legal.version_legal(juego("v1")) == legal.version_legal(juego("v1"))
    assert legal.version_legal(juego("v1")) != legal.version_legal(juego("v2"))
    assert legal.faltantes({"dpa": juego("v1")["dpa"]}) == ["terminos", "privacidad"]


def test_el_archivo_de_dominios_desechables_no_tiene_basura() -> None:
    from core.provisioning.antiabuso import _dominios_desechables

    dominios = _dominios_desechables()
    assert len(dominios) > 30
    assert all("." in d and " " not in d and d == d.lower() for d in dominios)
