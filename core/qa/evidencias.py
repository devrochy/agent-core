"""Capturas de los pasos de prueba (D-SPEC-30-4).

Viven en `TENANTS_ROOT/.plataforma/pruebas/<run_id>/`, el volumen persistente que ya
guarda las evidencias de cobranza (cubierto por el backup). El punto inicial evita
choques con cualquier slug de negocio (`[a-zA-Z0-9_-]+`). **No se guardan blobs en
Postgres.** El nombre físico es un `uuid4` (nunca el que manda el navegador) y la
ruta se resuelve siempre contra la raíz con chequeo de traversal.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from core.onboarding import tenants_root

MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}
MAX_BYTES = 5 * 1024 * 1024
MAX_POR_PASO = 10


class EvidenciaInvalida(ValueError):
    """Archivo rechazado (tipo, tamaño o vacío)."""


def raiz() -> Path:
    return tenants_root() / ".plataforma" / "pruebas"


def tipo_normalizado(mimetype: str | None) -> str:
    return (mimetype or "").split(";")[0].strip().lower()


def guardar(run_id: uuid.UUID, mimetype: str | None, contenido: bytes) -> tuple[str, str, int]:
    """Guarda la captura y devuelve `(ruta_relativa, mimetype, tamaño)`."""
    tipo = tipo_normalizado(mimetype)
    ext = MIME_EXT.get(tipo)
    if ext is None:
        raise EvidenciaInvalida("Tipo de archivo no admitido: usa jpeg, png, webp o pdf.")
    if not contenido:
        raise EvidenciaInvalida("El archivo está vacío.")
    if len(contenido) > MAX_BYTES:
        raise EvidenciaInvalida("El archivo supera 5 MB.")
    carpeta = raiz() / str(run_id)
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre = f"{uuid.uuid4().hex}{ext}"
    (carpeta / nombre).write_bytes(contenido)
    return f"{run_id}/{nombre}", tipo, len(contenido)


def resolver(ruta_relativa: str) -> Path | None:
    """Ruta absoluta de una captura guardada; `None` si escapa de la raíz o no existe."""
    base = raiz().resolve()
    destino = (base / (ruta_relativa or "")).resolve()
    if base not in destino.parents:
        return None
    return destino if destino.is_file() else None


def borrar(ruta_relativa: str) -> None:
    destino = resolver(ruta_relativa)
    if destino is not None:
        destino.unlink(missing_ok=True)
