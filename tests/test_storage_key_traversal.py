"""Hueco de path traversal entre negocios en la foto del producto (auditoría 2026-10-25).

`image_key = "<mi-slug>/productos/../../<otro-slug>/…"` pasaba el chequeo de prefijo, firmaba
una URL del archivo ajeno y, al reemplazar la foto, podía borrarlo. Aquí: el controlador de
almacenamiento (`key_limpia`, `dentro_de`) y la validación de `pack_commerce`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pack_commerce.admin_api import _key_producto_valida
from pack_commerce.fotos import key_foto_valida, url_foto

import core.almacenamiento as almacenamiento
from core import onboarding

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
PROPIO = "tienda-a"
AJENO = "tienda-b"
HOSTILES = [
    f"{PROPIO}/productos/../../{AJENO}/productos/secreto.png",
    f"{PROPIO}/productos/../../{AJENO}/conocimiento/doc.pdf",
    f"{PROPIO}/productos/./foto.png",
    f"{PROPIO}/productos//foto.png",
    f"{PROPIO}/productos/..\\..\\{AJENO}\\productos\\secreto.png",
    f"/{PROPIO}/productos/foto.png",
    "/etc/passwd",
    f"{PROPIO}/productos/foto.png\x00.png",
    f"{PROPIO}/productos/",
    "..",
    "",
]


@pytest.fixture
def raiz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(almacenamiento, "tenants_root", lambda: tmp_path)
    monkeypatch.setattr(almacenamiento, "carpeta_del_tenant", lambda slug: tmp_path / slug)
    monkeypatch.setattr(onboarding, "tenants_root", lambda: tmp_path)
    for slug in (PROPIO, AJENO):
        (tmp_path / slug / "productos").mkdir(parents=True)
        (tmp_path / slug / "productos" / "foto.png").write_bytes(PNG)
    (tmp_path / AJENO / "productos" / "secreto.png").write_bytes(PNG)
    (tmp_path / AJENO / "conocimiento").mkdir()
    (tmp_path / AJENO / "conocimiento" / "doc.pdf").write_bytes(b"%PDF-1.4")
    return tmp_path


def test_key_limpia_acepta_las_que_genera_guardar_y_rechaza_trucos() -> None:
    assert almacenamiento.key_limpia(f"{PROPIO}/productos/abc123.png")
    assert almacenamiento.key_limpia("avatares/9f8e.jpg")
    for hostil in HOSTILES:
        assert not almacenamiento.key_limpia(hostil), repr(hostil)
    assert not almacenamiento.key_limpia(None)


def test_abrir_y_borrar_ignoran_keys_con_traversal(raiz: Path) -> None:
    objetivo = raiz / AJENO / "productos" / "secreto.png"
    for hostil in HOSTILES:
        assert almacenamiento.abrir(hostil) is None, repr(hostil)
        almacenamiento.borrar(hostil)
    assert objetivo.is_file()


def test_dentro_de_cierra_el_paso_a_otro_negocio(raiz: Path) -> None:
    carpeta = almacenamiento.carpeta_de("producto", PROPIO)
    assert carpeta == f"{PROPIO}/productos"
    assert almacenamiento.abrir(f"{PROPIO}/productos/foto.png", dentro_de=carpeta) is not None
    # Key limpia pero de otro negocio: sin traversal, solo por la carpeta.
    assert almacenamiento.abrir(f"{AJENO}/productos/secreto.png", dentro_de=carpeta) is None
    almacenamiento.borrar(f"{AJENO}/productos/secreto.png", dentro_de=carpeta)
    assert (raiz / AJENO / "productos" / "secreto.png").is_file()
    # `dentro_de` hostil o que apunta a la raíz no abre nada.
    for dentro in ("..", ".", "", f"{PROPIO}/../{AJENO}"):
        assert almacenamiento.abrir(f"{AJENO}/productos/secreto.png", dentro_de=dentro) is None, dentro


def test_enlace_simbolico_hacia_otro_negocio_no_se_sirve_ni_borra(raiz: Path) -> None:
    (raiz / PROPIO / "productos" / "enlace.png").symlink_to(raiz / AJENO / "productos" / "secreto.png")
    carpeta = almacenamiento.carpeta_de("producto", PROPIO)
    key = f"{PROPIO}/productos/enlace.png"
    assert almacenamiento.abrir(key, dentro_de=carpeta) is None
    almacenamiento.borrar(key, dentro_de=carpeta)
    assert (raiz / AJENO / "productos" / "secreto.png").is_file()


def test_borrar_no_toca_carpetas(raiz: Path) -> None:
    almacenamiento.borrar(f"{AJENO}/productos")
    almacenamiento.borrar(AJENO)
    assert (raiz / AJENO / "productos" / "secreto.png").is_file()


def test_borrar_con_key_valida_borra_solo_ese_archivo(raiz: Path) -> None:
    carpeta = almacenamiento.carpeta_de("producto", PROPIO)
    almacenamiento.borrar(f"{PROPIO}/productos/foto.png", dentro_de=carpeta)
    assert not (raiz / PROPIO / "productos" / "foto.png").exists()
    assert (raiz / AJENO / "productos" / "foto.png").is_file()


def test_key_producto_valida_acepta_la_foto_propia(raiz: Path) -> None:
    assert _key_producto_valida(f"{PROPIO}/productos/foto.png", PROPIO)


@pytest.mark.parametrize("hostil", HOSTILES)
def test_key_producto_valida_rechaza_traversal_y_rutas_raras(raiz: Path, hostil: str) -> None:
    assert not _key_producto_valida(hostil, PROPIO)


def test_key_producto_valida_rechaza_otro_slug_aunque_exista(raiz: Path) -> None:
    assert not _key_producto_valida(f"{AJENO}/productos/secreto.png", PROPIO)
    assert not _key_producto_valida(f"{PROPIO}/productos/foto.png", None)
    assert not _key_producto_valida(f"{PROPIO}/productos/no-existe.png", PROPIO)
    # Prefijo parecido: `tienda-a-2` no es `tienda-a`.
    assert not _key_producto_valida("tienda-a-2/productos/foto.png", PROPIO)


def test_url_foto_no_firma_una_key_hostil_ya_guardada(raiz: Path) -> None:
    hostil = SimpleNamespace(image_key=HOSTILES[0], image_url="https://cdn.ejemplo.com/a.jpg")
    assert url_foto(hostil) == "https://cdn.ejemplo.com/a.jpg"
    sin_externa = SimpleNamespace(image_key=HOSTILES[0], image_url=None)
    assert url_foto(sin_externa) is None
    buena = SimpleNamespace(image_key=f"{PROPIO}/productos/foto.png", image_url=None)
    assert (url_foto(buena) or "").startswith("http") or "/api/v1/files/" in (url_foto(buena) or "")


def test_key_foto_valida_forma() -> None:
    assert key_foto_valida(f"{PROPIO}/productos/foto.png")
    assert key_foto_valida(f"{PROPIO}/productos/foto.png", PROPIO)
    assert not key_foto_valida(f"{AJENO}/productos/foto.png", PROPIO)
    assert not key_foto_valida(f"{PROPIO}/conocimiento/doc.pdf")
    assert not key_foto_valida("avatares/x.png")
    assert not key_foto_valida(None)
