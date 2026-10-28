"""Render de textos con variables, sin base ni red (ADR-0108, ADR-0172, ADR-0178 D1).

Tres dialectos, tres políticas para un dato que falta. D1 NO las unifica (cambiaría lo que ya sale a los
clientes): cada una conserva su comportamiento y sus llamadores eligen la suya.

- `render_fallback`: `{ruta}` de dos tramos y SOLO rutas permitidas. Una ruta no permitida queda tal cual; una
  permitida sin dato se reemplaza por vacío (texto de respaldo de un aviso por evento).
- `render_llaves`: `{ruta}` de cualquier forma. Sin valor, queda tal cual (plantillas con opciones).
- `render_meta`: `{{n}}` / `{{nombre}}` de las plantillas de Meta. Sin valor, queda tal cual. NO sanea: quien
  llama pasa valores ya saneados.

Todos sustituyen en una sola pasada: una `{` o `}` dentro de un VALOR no se vuelve a interpretar.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Any

from core.kernel.mensajes.rutas import valor_de_ruta
from core.kernel.mensajes.saneo import clean_param

# `{cliente.nombre}`: dos tramos en minúscula (el dialecto de los avisos por evento).
LLAVE_DE_RUTA = re.compile(r"\{([a-z_]+\.[a-z_]+)\}")
# `{ruta}` o `{x}`: cualquier cosa sin llaves dentro (el dialecto de las plantillas con opciones).
LLAVE = re.compile(r"\{([^{}]*)\}")
# `{{1}}`, `{{ nombre }}`: placeholder de una plantilla de Meta.
PLACEHOLDER_META = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


def render_fallback(texto: str, contexto: Mapping[str, Any], permitidas: Collection[str]) -> str:
    """Texto de respaldo con `{ruta}` reemplazadas por datos del contexto (solo rutas permitidas)."""

    def _sub(m: re.Match[str]) -> str:
        ruta = m.group(1)
        if ruta not in permitidas:
            return m.group(0)
        return valor_de_ruta(contexto, ruta) or ""

    return LLAVE_DE_RUTA.sub(_sub, texto)


def render_llaves(texto: str, valores: Mapping[str, str]) -> str:
    """Sustituye cada `{ruta}` por su valor saneado; una ruta sin valor queda como está."""
    return LLAVE.sub(
        lambda m: clean_param(valores[m.group(1)]) if m.group(1) in valores else m.group(0), texto
    )


def render_meta(texto: str, valores: Mapping[str, str]) -> str:
    """Sustituye cada `{{n}}` por `valores[n]`; sin valor queda como está. No sanea."""
    return PLACEHOLDER_META.sub(lambda m: valores.get(m.group(1), m.group(0)), texto)
