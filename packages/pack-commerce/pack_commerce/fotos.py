"""URL pública de la foto de un producto (HU-CRM-01 y HU-CRM-05).

Si el producto tiene `image_key` (foto subida al controlador único, ADR-0085 enmendado), se
devuelve una URL firmada de vida corta (60 min, suficiente para que Meta la descargue al
enviar). Si no, el link externo de `image_url`. Así el panel, la APK y el agente ven siempre
una URL usable.
"""

from __future__ import annotations

from typing import Any

from core import almacenamiento


def url_foto(product: Any) -> str | None:
    """URL de la foto del producto (firmada si se subió, externa si es un link)."""
    if getattr(product, "image_key", None):
        return almacenamiento.url(product.image_key, almacenamiento.URL_TTL_CANAL)
    return (getattr(product, "image_url", None) or "").strip() or None
