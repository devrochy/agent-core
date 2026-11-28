"""Tipos, áreas y límites del formulario de reportes (ADR-0213 §5).

Las áreas son las secciones de permiso del catálogo (core y packs, ADR-0136) más `otra`: el core no nombra áreas de
los packs. Se excluyen las de plataforma (`tenants`, `qa`), que un negocio no ve. El servidor valida `area` contra
esta misma lista.
"""

from __future__ import annotations

from core.almacenamiento import PERFILES

TIPOS: dict[str, str] = {"error": "Error", "mejora": "Mejora", "idea": "Idea"}
AREA_OTRA = "otra"

MAX_ADJUNTOS = 5
MAX_BYTES_POR_ARCHIVO = PERFILES["feedback"].max_bytes
MAX_BYTES_TOTAL = 25 * 1024 * 1024
MIMETYPES = tuple(sorted(PERFILES["feedback"].mimes))


def areas() -> dict[str, str]:
    """`{clave: etiqueta}` de las áreas que se pueden reportar, con `otra` al final."""
    from core.admin.permissions import GLOBAL_ONLY_SECTIONS, catalog, pack_section_keys
    from core.plugin_manager import plugin_manager

    secciones = set(plugin_manager.active_ids()) | pack_section_keys(plugin_manager.list_modules())
    resultado = {clave: etiqueta for clave, etiqueta in catalog(secciones).items() if clave not in GLOBAL_ONLY_SECTIONS}
    resultado[AREA_OTRA] = "Otra"
    return resultado
