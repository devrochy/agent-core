"""Lectura pura de los componentes de una plantilla de Meta (ADR-0108, ADR-0178 D1).

Trabaja sobre la lista de componentes (`[{"type": "BODY", "text": "Hola {{1}}"}, …]`), sin la fila de base:
quien tiene el `MessageTemplate` pasa su `components`.
"""

from __future__ import annotations

from typing import Any

from core.kernel.mensajes.render import PLACEHOLDER_META


def componente(componentes: Any, tipo: str) -> dict[str, Any] | None:
    """El primer componente de ese `type` (sin distinguir mayúsculas), o None."""
    for comp in componentes or []:
        if isinstance(comp, dict) and str(comp.get("type", "")).upper() == tipo:
            return comp
    return None


def placeholders_del_cuerpo(componentes: Any) -> list[str]:
    """Placeholders del cuerpo en orden de aparición, sin repetir ("1", "2" o nombres)."""
    body = componente(componentes, "BODY")
    vistos: list[str] = []
    for nombre in PLACEHOLDER_META.findall(str((body or {}).get("text", ""))):
        if nombre not in vistos:
            vistos.append(nombre)
    return vistos


def tiene_variables_fuera_del_cuerpo(componentes: Any) -> bool:
    """Cabecera o botones con variables: las integraciones solo resuelven las del cuerpo."""
    for comp in componentes or []:
        if not isinstance(comp, dict) or str(comp.get("type", "")).upper() == "BODY":
            continue
        if PLACEHOLDER_META.search(str(comp.get("text", ""))):
            return True
        for boton in comp.get("buttons") or []:
            if isinstance(boton, dict) and PLACEHOLDER_META.search(str(boton.get("url", ""))):
                return True
    return False
