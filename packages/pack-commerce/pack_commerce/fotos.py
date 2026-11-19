"""URL pública de la foto de un producto (HU-CRM-01 y HU-CRM-05).

Si el producto tiene `image_key` (foto subida al controlador único, ADR-0085 enmendado), se
devuelve una URL firmada de vida corta (60 min, suficiente para que Meta la descargue al
enviar). Si no, el link externo de `image_url`. Así el panel, la APK y el agente ven siempre
una URL usable.
"""

from __future__ import annotations

from typing import Any

from core import almacenamiento


def key_foto_valida(key: str | None, slug: str | None = None) -> bool:
    """`True` si `key` es una foto de producto bien formada: `<slug>/productos/<archivo>`, sin
    `..` ni rutas absolutas. Con `slug` además exige que sea de ese negocio. No mira el disco."""
    if not almacenamiento.key_limpia(key):
        return False
    partes = (key or "").split("/")
    carpeta = almacenamiento.PERFILES["producto"].carpeta
    if len(partes) < 3 or partes[1] != carpeta:
        return False
    return slug is None or partes[0] == slug


def url_foto(product: Any) -> str | None:
    """URL de la foto del producto (firmada si se subió, externa si es un link).

    Una `image_key` mal formada (dato viejo o hostil) no se firma: se cae al link externo."""
    key = getattr(product, "image_key", None)
    if key and key_foto_valida(key):
        return almacenamiento.url(key, almacenamiento.URL_TTL_CANAL)
    return (getattr(product, "image_url", None) or "").strip() or None
