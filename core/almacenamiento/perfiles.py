"""Perfiles de archivo del controlador único de almacenamiento (ADR-0085 enmendado).

Cada `kind` declara lo que se puede subir: tipos MIME admitidos, tamaño máximo, carpeta bajo
`TENANTS_ROOT` y si el archivo es de un negocio o de la plataforma. La validación de verdad
(magic bytes) vive aquí y no en cada feature: QA, conocimiento, productos y media comparten
el mismo contrato.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

MB = 1024 * 1024

MIME_JPEG = "image/jpeg"
MIME_PNG = "image/png"
MIME_WEBP = "image/webp"
MIME_GIF = "image/gif"
MIME_PDF = "application/pdf"
MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_TXT = "text/plain"
MIME_MD = "text/markdown"
MIME_MP3 = "audio/mpeg"
MIME_OGG = "audio/ogg"
MIME_WAV = "audio/wav"
MIME_M4A = "audio/mp4"
MIME_MP4 = "video/mp4"
MIME_3GPP = "video/3gpp"

_IMAGENES = frozenset({MIME_JPEG, MIME_PNG, MIME_WEBP, MIME_GIF})
_DOCUMENTOS = frozenset({MIME_PDF, MIME_DOCX, MIME_TXT, MIME_MD})
_AUDIO = frozenset({MIME_MP3, MIME_OGG, MIME_WAV, MIME_M4A})
_VIDEO = frozenset({MIME_MP4, MIME_3GPP})

_EXTENSION = {
    MIME_JPEG: ".jpg",
    MIME_PNG: ".png",
    MIME_WEBP: ".webp",
    MIME_GIF: ".gif",
    MIME_PDF: ".pdf",
    MIME_DOCX: ".docx",
    MIME_TXT: ".txt",
    MIME_MD: ".md",
    MIME_MP3: ".mp3",
    MIME_OGG: ".ogg",
    MIME_WAV: ".wav",
    MIME_M4A: ".m4a",
    MIME_MP4: ".mp4",
    MIME_3GPP: ".3gp",
}

_POR_EXTENSION = {
    ".jpg": MIME_JPEG,
    ".jpeg": MIME_JPEG,
    ".png": MIME_PNG,
    ".webp": MIME_WEBP,
    ".gif": MIME_GIF,
    ".pdf": MIME_PDF,
    ".docx": MIME_DOCX,
    ".txt": MIME_TXT,
    ".md": MIME_MD,
    ".markdown": MIME_MD,
    ".mp3": MIME_MP3,
    ".ogg": MIME_OGG,
    ".wav": MIME_WAV,
    ".m4a": MIME_M4A,
    ".mp4": MIME_MP4,
    ".3gp": MIME_3GPP,
}

_GENERICOS = {"", "application/octet-stream", "binary/octet-stream"}


class StorageInvalido(ValueError):
    """Archivo rechazado. `codigo` va al error de la API; el mensaje es para el dueño."""

    def __init__(self, codigo: str, mensaje: str) -> None:
        self.codigo = codigo
        super().__init__(mensaje)


@dataclass(frozen=True)
class Perfil:
    kind: str
    carpeta: str
    mimes: frozenset[str]
    max_bytes: int
    tenant_scoped: bool = True
    retencion_dias: int | None = None


# La carpeta es relativa a `TENANTS_ROOT/<slug>/` (o a `TENANTS_ROOT/` si no es por negocio).
# `qa` mantiene la ruta histórica `.plataforma/pruebas/<run_id>/` para no mover evidencias.
PERFILES: dict[str, Perfil] = {
    "producto": Perfil("producto", "productos", _IMAGENES, 5 * MB),
    "conocimiento": Perfil("conocimiento", "conocimiento", _DOCUMENTOS, 20 * MB),
    "qa": Perfil("qa", ".plataforma/pruebas", _IMAGENES | {MIME_PDF}, 5 * MB, tenant_scoped=False),
    "media": Perfil("media", "media", _IMAGENES | _AUDIO | _VIDEO | {MIME_PDF}, 25 * MB),
    "comprobante": Perfil("comprobante", "comprobantes", _IMAGENES | {MIME_PDF}, 10 * MB),
}


def perfil(kind: str) -> Perfil:
    encontrado = PERFILES.get(kind)
    if encontrado is None:
        raise StorageInvalido("tipo_desconocido", f"Tipo de archivo desconocido: {kind}.")
    return encontrado


def _normalizado(mime: str | None) -> str:
    return (mime or "").split(";")[0].strip().lower()


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
    except UnicodeDecodeError:
        try:
            muestra.decode("latin-1")
        except UnicodeDecodeError:
            return False
        imprimibles = sum(1 for b in muestra if b >= 32 or b in (9, 10, 13))
        return imprimibles / max(1, len(muestra)) > 0.95


def _firma_coincide(tipo: str, contenido: bytes) -> bool:
    """Los primeros bytes confirman el tipo declarado (no se confía solo en el navegador)."""
    if tipo == MIME_JPEG:
        return contenido.startswith(b"\xff\xd8\xff")
    if tipo == MIME_PNG:
        return contenido.startswith(b"\x89PNG\r\n\x1a\n")
    if tipo == MIME_WEBP:
        return contenido[:4] == b"RIFF" and contenido[8:12] == b"WEBP"
    if tipo == MIME_GIF:
        return contenido[:6] in (b"GIF87a", b"GIF89a")
    if tipo == MIME_PDF:
        return contenido.startswith(b"%PDF-")
    if tipo == MIME_DOCX:
        return _es_docx(contenido)
    if tipo in (MIME_TXT, MIME_MD):
        return _es_texto(contenido)
    if tipo in (MIME_MP3, MIME_OGG, MIME_WAV, MIME_M4A, MIME_MP4, MIME_3GPP):
        # Audio/video: el contenedor no tiene una firma única fiable; se acepta el MIME.
        return True
    return False


def resolver_tipo(perfil_: Perfil, mime: str | None, nombre: str | None) -> str:
    """MIME admitido, por el declarado o (si es genérico) por la extensión del nombre."""
    declarado = _normalizado(mime)
    if declarado in perfil_.mimes:
        return declarado
    extension = ("." + (nombre or "").rsplit(".", 1)[-1].lower()) if nombre and "." in nombre else ""
    por_extension = _POR_EXTENSION.get(extension)
    if por_extension in perfil_.mimes and (declarado in _GENERICOS or declarado.startswith("text/")):
        return por_extension
    admitidos = ", ".join(sorted(perfil_.mimes))
    raise StorageInvalido("formato", f"Tipo de archivo no admitido. Se aceptan: {admitidos}.")


def validar(perfil_: Perfil, mime: str | None, nombre: str | None, contenido: bytes) -> tuple[str, str]:
    """Valida y devuelve `(mime, extensión)`. Lanza `StorageInvalido` si no sirve."""
    if not contenido:
        raise StorageInvalido("vacio", "El archivo está vacío.")
    if len(contenido) > perfil_.max_bytes:
        limite = perfil_.max_bytes // MB
        raise StorageInvalido("muy_grande", f"El archivo supera {limite} MB.")
    tipo = resolver_tipo(perfil_, mime, nombre)
    if not _firma_coincide(tipo, contenido):
        raise StorageInvalido("formato", "El contenido no corresponde al tipo de archivo.")
    return tipo, _EXTENSION[tipo]
