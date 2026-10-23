"""Errores de proveedores de media (SPEC-04, ADR-0151).

El pipeline distingue transitorio (red, timeout, 429, 5xx) de permanente
(401/403/400): el primero se reintenta por el consumer; el segundo responde el
mensaje configurado y no se repite en vano.
"""

from __future__ import annotations


class MediaProviderError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False, code: str = "media_provider_error") -> None:
        super().__init__(message)
        self.transient = transient
        self.code = code


def error_from_status(status_code: int, detail: str) -> MediaProviderError:
    """Clasifica una respuesta HTTP del proveedor sin filtrar la llave."""
    transient = status_code == 429 or status_code >= 500
    code = "media_rate_limited" if status_code == 429 else "media_provider_error"
    return MediaProviderError(
        f"El proveedor de media respondió HTTP {status_code}: {detail[:300]}",
        transient=transient,
        code=code,
    )


__all__ = ["MediaProviderError", "error_from_status"]
