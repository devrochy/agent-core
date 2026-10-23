"""Prompts de media desde el playbook del kernel (invariante 3, ADR-0151).

No hay prompts en este archivo: solo el parser de secciones `## nombre` del
playbook versionado `core/playbooks/kernel/media.md`.
"""

from __future__ import annotations

from core.agents.playbooks import load_core_playbook

PLAYBOOK = "media"


def prompt_de_media(seccion: str, **variables: str) -> str | None:
    """Devuelve la sección pedida con `{variables}` reemplazadas; `None` si no existe."""
    contenido = load_core_playbook(PLAYBOOK).strip()
    if contenido.startswith("## "):
        contenido = contenido[3:]
    for bloque in contenido.split("\n## "):
        cabecera, _, cuerpo = bloque.partition("\n")
        if cabecera.strip() != seccion:
            continue
        texto = cuerpo.strip()
        for clave, valor in variables.items():
            texto = texto.replace("{" + clave + "}", valor or "")
        return texto or None
    return None


__all__ = ["prompt_de_media"]
