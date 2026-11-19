"""Archivos originales del conocimiento (ADR-0145 §2 y §6).

Viven en `TENANTS_ROOT/<slug>/conocimiento/<document_id>.<ext>`, el mismo volumen persistente de
las evidencias, cubierto por el backup. No hay blobs en Postgres.

- El tipo se decide por el MIME declarado **o** por la extensión del nombre: los navegadores
  mandan `.md` como `application/octet-stream`. Después, el contenido tiene que confirmarlo:
  magic bytes del PDF, un ZIP con `word/document.xml` para DOCX y texto sin NUL para TXT/MD.
- El nombre físico lo pone el servidor (el id del documento) y la ruta se resuelve siempre
  contra la carpeta del tenant, con chequeo de traversal: `../`, una ruta absoluta u otro slug no
  resuelven a nada.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path

from core import almacenamiento
from core.onboarding import carpeta_del_tenant

MIME_PDF = "application/pdf"
MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_TXT = "text/plain"
MIME_MD = "text/markdown"

EXTENSIONES = {MIME_PDF: ".pdf", MIME_DOCX: ".docx", MIME_TXT: ".txt", MIME_MD: ".md"}
_POR_EXTENSION = {".pdf": MIME_PDF, ".docx": MIME_DOCX, ".txt": MIME_TXT, ".md": MIME_MD, ".markdown": MIME_MD}
_GENERICOS = {"", "application/octet-stream", "binary/octet-stream"}

MAX_BYTES = 20 * 1024 * 1024
CARPETA = "conocimiento"


class ArchivoInvalido(ValueError):
    """Archivo rechazado. `codigo` va al 422 de la API; el mensaje es para el dueño."""

    def __init__(self, codigo: str, mensaje: str) -> None:
        self.codigo = codigo
        super().__init__(mensaje)


def _normalizado(mime: str | None) -> str:
    return (mime or "").split(";")[0].strip().lower()


def tipo_de(mime: str | None, nombre: str | None) -> str:
    """MIME admitido del archivo, por el declarado o, si es genérico, por la extensión."""
    declarado = _normalizado(mime)
    if declarado in EXTENSIONES:
        return declarado
    extension = Path(nombre or "").suffix.lower()
    por_extension = _POR_EXTENSION.get(extension)
    if por_extension is not None and (declarado in _GENERICOS or declarado.startswith("text/")):
        return por_extension
    raise ArchivoInvalido("formato", "Tipo de archivo no admitido: sube un PDF, DOCX, TXT o Markdown.")


def _es_docx(contenido: bytes) -> bool:
    if not contenido.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(contenido)) as archivo:
            return "word/document.xml" in archivo.namelist()
    except zipfile.BadZipFile:
        return False


def _es_texto(contenido: bytes) -> bool:
    muestra = contenido[:65536]
    if b"\x00" in muestra:
        return False
    try:
        muestra.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        # Un carácter multibyte cortado al final de la muestra no es binario.
        if exc.start >= len(muestra) - 3:
            return True
        try:
            muestra.decode("latin-1")
        except UnicodeDecodeError:
            return False
        # latin-1 decodifica cualquier byte: se exige que casi todo sea imprimible.
        imprimibles = sum(1 for b in muestra if b >= 32 or b in (9, 10, 13))
        return imprimibles / max(1, len(muestra)) > 0.95


def validar(tipo: str, contenido: bytes) -> None:
    if not contenido:
        raise ArchivoInvalido("vacio", "El archivo está vacío.")
    if len(contenido) > MAX_BYTES:
        raise ArchivoInvalido("muy_grande", "El archivo supera 20 MB.")
    if tipo == MIME_PDF:
        valido = contenido.startswith(b"%PDF-")
    elif tipo == MIME_DOCX:
        valido = _es_docx(contenido)
    else:
        valido = _es_texto(contenido)
    if not valido:
        raise ArchivoInvalido("formato", "El contenido no corresponde al tipo de archivo.")


def carpeta(slug: str) -> Path:
    # Slug validado y dentro de TENANTS_ROOT (`onboarding.carpeta_del_tenant`).
    return carpeta_del_tenant(slug) / CARPETA


def guardar(slug: str, document_id: uuid.UUID, tipo: str, contenido: bytes) -> str:
    """Escribe el original (vía el controlador) y devuelve la ruta relativa a `carpeta(slug)`."""
    try:
        ref = almacenamiento.guardar(
            "conocimiento", contenido, mimetype=tipo, tenant_slug=slug, nombre_base=str(document_id)
        )
    except almacenamiento.StorageInvalido as exc:
        raise ArchivoInvalido(exc.codigo, str(exc)) from exc
    return ref.key.rsplit("/", 1)[-1]


def resolver(slug: str, ruta_relativa: str | None) -> Path | None:
    """Ruta absoluta de un original; `None` si escapa de la carpeta del tenant o no existe."""
    if not ruta_relativa:
        return None
    base = carpeta(slug).resolve()
    destino = (base / ruta_relativa).resolve()
    if destino.parent != base:
        return None
    return destino if destino.is_file() else None


def borrar(slug: str, ruta_relativa: str | None) -> None:
    destino = resolver(slug, ruta_relativa)
    if destino is not None:
        destino.unlink(missing_ok=True)


__all__ = [
    "MAX_BYTES",
    "MIME_DOCX",
    "MIME_MD",
    "MIME_PDF",
    "MIME_TXT",
    "ArchivoInvalido",
    "borrar",
    "carpeta",
    "guardar",
    "resolver",
    "tipo_de",
    "validar",
]
