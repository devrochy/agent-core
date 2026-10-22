"""Capturas de los pasos de prueba (D-SPEC-30-4, HU-ALM-02).

Viven en `TENANTS_ROOT/.plataforma/pruebas/<run_id>/`, el perfil `qa` del controlador único
(ADR-0085 enmendado). El controlador valida tipo/tamaño y escribe; este módulo solo resuelve
la ruta y la limita a su carpeta. **No se guardan blobs en Postgres.** El nombre físico lo pone
el servidor y la ruta se resuelve siempre contra la raíz con chequeo de traversal.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from core import almacenamiento
from core.onboarding import tenants_root

MAX_POR_PASO = 10
_PREFIJO = ".plataforma/pruebas/"
# Compatibilidad con `core/api/routers/qa.py`: el límite es el del perfil `qa`.
MAX_BYTES = almacenamiento.PERFILES["qa"].max_bytes


class EvidenciaInvalida(ValueError):
    """Archivo rechazado (tipo, tamaño o vacío)."""


def raiz() -> Path:
    return tenants_root() / ".plataforma" / "pruebas"


def guardar(run_id: uuid.UUID, mimetype: str | None, contenido: bytes) -> tuple[str, str, int]:
    """Guarda la captura (vía el controlador) y devuelve `(ruta_relativa, mimetype, tamaño)`."""
    try:
        ref = almacenamiento.guardar("qa", contenido, mimetype=mimetype, subcarpeta=str(run_id))
    except almacenamiento.StorageInvalido as exc:
        raise EvidenciaInvalida(str(exc)) from exc
    ruta = ref.key[len(_PREFIJO):] if ref.key.startswith(_PREFIJO) else ref.key
    return ruta, ref.mimetype, ref.bytes


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
