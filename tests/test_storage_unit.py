"""Controlador de almacenamiento: validación por perfil y URLs firmadas (HU-ALM-01)."""

from __future__ import annotations

import io
import zipfile

import pytest

from core.almacenamiento import abrir, firmas, url
from core.almacenamiento.perfiles import PERFILES, StorageInvalido, validar

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPEG = b"\xff\xd8\xff" + b"0" * 32
PDF = b"%PDF-1.7\n" + b"0" * 32
TXT = b"hola mundo"


def _docx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archivo:
        archivo.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


def test_producto_acepta_imagen_y_rechaza_pdf() -> None:
    tipo, extension = validar(PERFILES["producto"], "image/png", "foto.png", PNG)
    assert (tipo, extension) == ("image/png", ".png")
    with pytest.raises(StorageInvalido):
        validar(PERFILES["producto"], "application/pdf", "doc.pdf", PDF)


def test_avatar_cliente_es_imagen_por_negocio() -> None:
    perfil = PERFILES["avatar_cliente"]
    assert perfil.tenant_scoped is True
    validar(perfil, "image/jpeg", "cliente.jpg", JPEG)
    with pytest.raises(StorageInvalido):
        validar(perfil, "application/pdf", "doc.pdf", PDF)


def test_conocimiento_acepta_pdf_docx_txt_y_rechaza_imagen() -> None:
    for mime, nombre, contenido in [
        ("application/pdf", "a.pdf", PDF),
        ("text/plain", "a.txt", TXT),
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "a.docx", _docx()),
    ]:
        validar(PERFILES["conocimiento"], mime, nombre, contenido)
    with pytest.raises(StorageInvalido):
        validar(PERFILES["conocimiento"], "image/png", "a.png", PNG)


def test_qa_mantiene_su_allowlist() -> None:
    validar(PERFILES["qa"], "image/jpeg", "cap.jpg", JPEG)
    validar(PERFILES["qa"], "application/pdf", "cap.pdf", PDF)
    with pytest.raises(StorageInvalido):
        validar(PERFILES["qa"], "text/plain", "nota.txt", TXT)


def test_tipo_por_extension_cuando_el_mime_es_generico() -> None:
    tipo, _ = validar(PERFILES["conocimiento"], "application/octet-stream", "notas.md", TXT)
    assert tipo == "text/markdown"


def test_rechaza_vacio_muy_grande_y_firma_incorrecta() -> None:
    with pytest.raises(StorageInvalido):
        validar(PERFILES["producto"], "image/png", "a.png", b"")
    with pytest.raises(StorageInvalido):
        validar(PERFILES["producto"], "image/png", "a.png", b"\x89PNG\r\n\x1a\n" + b"0" * (6 * 1024 * 1024))
    with pytest.raises(StorageInvalido):
        validar(PERFILES["producto"], "image/png", "a.png", b"no soy png")


def test_firma_valida_y_expira() -> None:
    token = firmas.firmar("tienda-norte/productos/x.png", 60, ahora=1000)
    assert firmas.verificar(token, ahora=1030) == "tienda-norte/productos/x.png"
    assert firmas.verificar(token, ahora=1100) is None


def test_firma_manipulada_no_pasa() -> None:
    token = firmas.firmar("a/b.png", 60, ahora=1000)
    key_b64, exp, _firma = token.split(".")
    cambiada = "A" if _firma[-1] != "A" else "B"
    assert firmas.verificar(f"{key_b64}.{exp}.{_firma[:-1]}{cambiada}", ahora=1030) is None
    assert firmas.verificar("basura", ahora=1030) is None


def test_url_firmada_apunta_al_servidor_de_archivos() -> None:
    from core.config import get_settings

    base = get_settings().public_base_url.rstrip("/")
    enlace = url("tienda/productos/x.png", ttl_s=60)
    assert enlace.startswith(f"{base}/api/v1/files/")
    token = enlace.rsplit("/", 1)[-1]
    assert firmas.verificar(token) == "tienda/productos/x.png"


def test_abrir_no_sale_de_tenants_root() -> None:
    assert abrir("../etc/passwd") is None
    assert abrir("/etc/passwd") is None
