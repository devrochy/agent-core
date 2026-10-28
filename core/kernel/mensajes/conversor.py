"""Conversor entre los dialectos de variables (ADR-0178 D1).

Un texto puede traer variables de tres formas, que este módulo reconoce y convierte SIN cambiar el significado:

- `{cliente.nombre}`: ruta de datos (avisos por evento, plantillas con opciones);
- `{nombre}`: llave simple (textos de los packs, que hoy se rellenan con `str.format`);
- `{{1}}` / `{{nombre}}`: placeholder de una plantilla de Meta.

Los packs NO usan este conversor todavía (D1 no toca su render): solo se reconoce su forma.
"""

from __future__ import annotations

import re

_CUALQUIERA = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}|\{([^{}]*)\}")


def nombre_de_parametro(ruta: str) -> str:
    """`cliente.nombre` → `cliente_nombre`: el nombre NAMED que Meta acepta (minúsculas, números, _)."""
    return ruta.replace(".", "_")


def variables_de(texto: str) -> list[str]:
    """Variables que cita el texto, en cualquiera de los tres dialectos, en orden y sin repetir:
    `{a.b}` → `a.b`, `{x}` → `x`, `{{n}}` → `n`. Una llave vacía no cuenta."""
    vistas: list[str] = []
    for m in _CUALQUIERA.finditer(texto):
        nombre = m.group(1) if m.group(1) is not None else m.group(2)
        if nombre and nombre not in vistas:
            vistas.append(nombre)
    return vistas


def a_llaves(texto: str) -> str:
    """`{{n}}` → `{n}`; lo que ya está con llaves simples no cambia."""
    return _CUALQUIERA.sub(
        lambda m: "{" + m.group(1) + "}" if m.group(1) is not None else m.group(0), texto
    )


def a_meta(texto: str) -> str:
    """`Hola {cliente.nombre}` → `Hola {{cliente_nombre}}`; un `{{n}}` que ya está en formato Meta no cambia."""
    return _CUALQUIERA.sub(
        lambda m: (
            m.group(0) if m.group(1) is not None else "{{" + nombre_de_parametro(m.group(2)) + "}}"
        ),
        texto,
    )
