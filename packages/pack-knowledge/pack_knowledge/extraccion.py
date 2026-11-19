"""Extracción de texto de PDF, DOCX y TXT/Markdown (ADR-0145 §2, HU-AOS-49 parte 2).

Devuelve bloques (párrafos o encabezados) con su sección y su página para que el troceado respete
la estructura y cada fragmento se pueda citar. Sin OCR: un PDF escaneado falla con `sin_texto`.

Los errores llevan un código del check `ck_pack_knowledge_documents_error` y nunca texto del
documento.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

from pack_knowledge.archivos import MIME_DOCX, MIME_MD, MIME_PDF, MIME_TXT

MAX_PAGINAS = 300
# Menos de esto, en todo el documento, es un PDF escaneado o vacío.
MIN_CARACTERES = 20
# Tope de lo extraído (~600.000 tokens): por encima, el documento no entra en los 2.000 fragmentos.
MAX_CARACTERES = 2_400_000

_ENCABEZADO_MD = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_BLANCOS = re.compile(r"[ \t\u00a0]+")


class ExtraccionError(Exception):
    def __init__(self, codigo: str, detalle: str | None = None) -> None:
        self.codigo = codigo
        self.detalle = detalle
        super().__init__(codigo if detalle is None else f"{codigo}: {detalle}")


@dataclass(frozen=True)
class Bloque:
    texto: str
    seccion: str | None = None
    pagina: int | None = None


@dataclass(frozen=True)
class Extraido:
    bloques: list[Bloque]
    paginas: int | None


def _limpio(texto: str) -> str:
    return _BLANCOS.sub(" ", texto.replace("\x00", "")).strip()


def _parrafos(texto: str) -> list[str]:
    """Párrafos separados por líneas en blanco; las líneas de un párrafo se unen."""
    salida: list[str] = []
    for crudo in re.split(r"\n\s*\n", texto.replace("\r\n", "\n").replace("\r", "\n")):
        parrafo = _limpio(" ".join(linea.strip() for linea in crudo.split("\n")))
        if parrafo:
            salida.append(parrafo)
    return salida


def _validar_total(bloques: list[Bloque]) -> None:
    total = sum(len(b.texto) for b in bloques)
    if total < MIN_CARACTERES:
        raise ExtraccionError("sin_texto")
    if total > MAX_CARACTERES:
        raise ExtraccionError("muy_grande", "texto")


# ------------------------------------------------------------------ PDF
def _pdf(contenido: bytes) -> Extraido:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        lector = PdfReader(io.BytesIO(contenido))
        if lector.is_encrypted and not lector.decrypt(""):
            raise ExtraccionError("formato", "cifrado")
        paginas = len(lector.pages)
    except ExtraccionError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, NotImplementedError):
        raise ExtraccionError("formato", "pdf_ilegible") from None
    if paginas > MAX_PAGINAS:
        raise ExtraccionError("muy_grande", "paginas")
    bloques: list[Bloque] = []
    for numero, pagina in enumerate(lector.pages, start=1):
        try:
            texto = pagina.extract_text() or ""
        except Exception:
            # Una página rota no tumba el documento; se registra solo el número.
            continue
        bloques.extend(Bloque(texto=p, pagina=numero) for p in _parrafos(texto))
    _validar_total(bloques)
    return Extraido(bloques=bloques, paginas=paginas)


# ------------------------------------------------------------------ DOCX
_ESTILOS_TITULO = ("heading", "título", "titulo", "title")


def _docx(contenido: bytes) -> Extraido:
    from docx import Document

    try:
        documento = Document(io.BytesIO(contenido))
    except (zipfile.BadZipFile, KeyError, ValueError):
        raise ExtraccionError("formato", "docx_ilegible") from None
    except Exception:
        raise ExtraccionError("formato", "docx_ilegible") from None
    bloques: list[Bloque] = []
    seccion: str | None = None
    for parrafo in documento.paragraphs:
        texto = _limpio(parrafo.text)
        if not texto:
            continue
        estilo = (parrafo.style.name if parrafo.style is not None else "").casefold()
        if estilo.startswith(_ESTILOS_TITULO):
            seccion = texto[:200]
            continue
        bloques.append(Bloque(texto=texto, seccion=seccion))
    # Las tablas (precios, horarios) van fila por fila, con sus celdas separadas por « | ».
    for tabla in documento.tables:
        for fila in tabla.rows:
            celdas = [_limpio(c.text) for c in fila.cells]
            texto = " | ".join(dict.fromkeys(c for c in celdas if c))
            if texto:
                bloques.append(Bloque(texto=texto, seccion="Tablas"))
    _validar_total(bloques)
    return Extraido(bloques=bloques, paginas=None)


# ------------------------------------------------------------------ TXT / Markdown
def decodificar(contenido: bytes) -> str:
    try:
        return contenido.decode("utf-8-sig")
    except UnicodeDecodeError:
        return contenido.decode("latin-1")


def _texto(contenido: bytes, *, markdown: bool) -> Extraido:
    texto = decodificar(contenido)
    bloques: list[Bloque] = []
    seccion: str | None = None
    actual: list[str] = []

    def _cerrar() -> None:
        bloques.extend(Bloque(texto=p, seccion=seccion) for p in _parrafos("\n".join(actual)))
        actual.clear()

    for linea in texto.replace("\r\n", "\n").split("\n"):
        encabezado = _ENCABEZADO_MD.match(linea) if markdown else None
        if encabezado:
            _cerrar()
            seccion = _limpio(encabezado.group(2))[:200] or None
            continue
        actual.append(linea)
    _cerrar()
    _validar_total(bloques)
    return Extraido(bloques=bloques, paginas=None)


def extraer_texto(mime: str, contenido: bytes) -> Extraido:
    if mime == MIME_PDF:
        return _pdf(contenido)
    if mime == MIME_DOCX:
        return _docx(contenido)
    if mime in (MIME_TXT, MIME_MD):
        return _texto(contenido, markdown=mime == MIME_MD)
    raise ExtraccionError("formato", "tipo")
