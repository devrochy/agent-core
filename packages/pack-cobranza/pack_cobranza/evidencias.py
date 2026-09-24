"""Almacenamiento de evidencias de cliente (foto facial / documento).

Las evidencias viven en `TENANTS_ROOT/<slug>/evidencias/<cliente_id>/`, la misma
raíz de artefactos del tenant que los playbooks por tenant (ADR-0026): gitignored
y cubierta por el backup del runbook. **No se guardan blobs en Postgres** — la BD
es de negocio, no de archivos.

El nombre físico es un `uuid4` (nunca el nombre que manda el cliente, que es dato
no confiable); la ruta se resuelve siempre contra la raíz del tenant con chequeo
de traversal.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from core.onboarding import tenant_dir
from core.shared.tenant import tenant_slug

TIPOS = ("foto_facial", "documento_frente", "documento_reverso")
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}
MAX_BYTES = 5 * 1024 * 1024


class EvidenciaError(ValueError):
    """Dato de evidencia inválido (tipo, mime o tamaño)."""


def _slug_seguro(slug: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", slug or ""):
        raise EvidenciaError("slug de tenant inválido")
    return slug


def _evidencias_root(slug: str) -> Path:
    return tenant_dir(_slug_seguro(slug)) / "evidencias"


async def guardar_evidencia(
    tenant_id: uuid.UUID,
    cliente_id: uuid.UUID,
    tipo: str,
    nombre_original: str | None,
    mimetype: str | None,
    contenido: bytes,
) -> dict[str, object]:
    """Guarda el archivo y devuelve los metadatos para `ClienteEvidencia`."""
    if tipo not in TIPOS:
        raise EvidenciaError("tipo de evidencia inválido")
    ext = _MIME_EXT.get((mimetype or "").split(";")[0].strip().lower())
    if ext is None:
        raise EvidenciaError("tipo de archivo no admitido (jpeg, png, webp o pdf)")
    if not contenido:
        raise EvidenciaError("archivo vacío")
    if len(contenido) > MAX_BYTES:
        raise EvidenciaError("archivo demasiado grande (máx. 5 MB)")
    slug = await tenant_slug(tenant_id)
    if not slug:
        raise EvidenciaError("tenant sin slug")
    base = _evidencias_root(slug) / str(cliente_id)
    base.mkdir(parents=True, exist_ok=True)
    nombre_fisico = f"{uuid.uuid4().hex}{ext}"
    (base / nombre_fisico).write_bytes(contenido)
    return {
        "ruta_archivo": f"{cliente_id}/{nombre_fisico}",
        "nombre_original": nombre_original,
        "mimetype": mimetype,
        "tamano": len(contenido),
    }


async def ruta_evidencia(tenant_id: uuid.UUID, ruta_relativa: str) -> Path | None:
    """Resuelve una ruta guardada contra la raíz del tenant. `None` si escapa o no existe."""
    slug = await tenant_slug(tenant_id)
    if not slug:
        return None
    try:
        base = _evidencias_root(slug).resolve()
    except EvidenciaError:
        return None
    destino = (base / (ruta_relativa or "")).resolve()
    if base != destino and base not in destino.parents:
        return None
    return destino if destino.is_file() else None
