"""Validador mínimo de JSON Schema para los argumentos de una tool (SPEC-40).

El repo no valida hoy los argumentos de un `tool_call` contra el schema de la tool (el LLM los arma y
cada handler se defiende), y `jsonschema` no es una dependencia. Las opciones SÍ lo necesitan: sus
argumentos los escribe el dueño en el panel y un valor fuera de tipo se atrapa al guardar, no cuando el
cliente toca el botón. Cubre el subconjunto que las tools usan: `type`, `enum`, `const`, rangos,
largos, `pattern`, `items`, `properties`, `required` y `additionalProperties: false`. `anyOf`, `oneOf`,
`allOf` y `$ref` no se evalúan (se aceptan): la ejecución (PR del resolver) vuelve a validar.

Puro.
"""

from __future__ import annotations

import re
from typing import Any

_TIPOS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}
_MAX_PROFUNDIDAD = 8


def errores(valor: Any, schema: Any, ruta: str = "", _prof: int = 0) -> list[tuple[str, str]]:
    """`(path, mensaje)` por cada incumplimiento de `valor` frente a `schema`. Lista vacía = cumple."""
    if not isinstance(schema, dict) or _prof > _MAX_PROFUNDIDAD:
        return []
    salida: list[tuple[str, str]] = []
    tipos = schema.get("type")
    if tipos is not None:
        lista = [tipos] if isinstance(tipos, str) else list(tipos) if isinstance(tipos, list) else []
        conocidos = [t for t in lista if t in _TIPOS]
        if conocidos and not any(_TIPOS[t](valor) for t in conocidos):
            return [(ruta, f"Debe ser de tipo {' o '.join(conocidos)}.")]
    if "const" in schema and valor != schema["const"]:
        salida.append((ruta, "No es el valor permitido."))
    if isinstance(schema.get("enum"), list) and valor not in schema["enum"]:
        salida.append((ruta, "No es uno de los valores permitidos."))
    if isinstance(valor, str):
        salida += _texto(valor, schema, ruta)
    elif isinstance(valor, int | float) and not isinstance(valor, bool):
        salida += _numero(valor, schema, ruta)
    elif isinstance(valor, list):
        salida += _lista(valor, schema, ruta, _prof)
    elif isinstance(valor, dict):
        salida += _objeto(valor, schema, ruta, _prof)
    return salida


def _texto(valor: str, schema: dict[str, Any], ruta: str) -> list[tuple[str, str]]:
    salida: list[tuple[str, str]] = []
    if isinstance(schema.get("minLength"), int) and len(valor) < schema["minLength"]:
        salida.append((ruta, f"Debe tener al menos {schema['minLength']} caracteres."))
    if isinstance(schema.get("maxLength"), int) and len(valor) > schema["maxLength"]:
        salida.append((ruta, f"Debe tener a lo más {schema['maxLength']} caracteres."))
    patron = schema.get("pattern")
    if isinstance(patron, str):
        try:
            if re.search(patron, valor) is None:
                salida.append((ruta, "No cumple el formato esperado."))
        except re.error:
            pass  # un patrón mal escrito de la tool no es culpa del dueño
    return salida


def _numero(valor: float, schema: dict[str, Any], ruta: str) -> list[tuple[str, str]]:
    salida: list[tuple[str, str]] = []
    for clave, cumple, texto in (
        ("minimum", lambda v, k: v >= k, "Debe ser al menos {}."),
        ("maximum", lambda v, k: v <= k, "Debe ser a lo más {}."),
        ("exclusiveMinimum", lambda v, k: v > k, "Debe ser mayor que {}."),
        ("exclusiveMaximum", lambda v, k: v < k, "Debe ser menor que {}."),
    ):
        limite = schema.get(clave)
        if isinstance(limite, int | float) and not isinstance(limite, bool) and not cumple(valor, limite):
            salida.append((ruta, texto.format(limite)))
    return salida


def _lista(valor: list[Any], schema: dict[str, Any], ruta: str, prof: int) -> list[tuple[str, str]]:
    salida: list[tuple[str, str]] = []
    if isinstance(schema.get("minItems"), int) and len(valor) < schema["minItems"]:
        salida.append((ruta, f"Necesita al menos {schema['minItems']} elementos."))
    if isinstance(schema.get("maxItems"), int) and len(valor) > schema["maxItems"]:
        salida.append((ruta, f"Admite a lo más {schema['maxItems']} elementos."))
    items = schema.get("items")
    if isinstance(items, dict):
        for i, v in enumerate(valor):
            salida += errores(v, items, f"{ruta}[{i}]", prof + 1)
    return salida


def _objeto(valor: dict[str, Any], schema: dict[str, Any], ruta: str, prof: int) -> list[tuple[str, str]]:
    salida: list[tuple[str, str]] = []
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for nombre in schema.get("required") or []:
        if nombre not in valor:
            salida.append((f"{ruta}.{nombre}" if ruta else str(nombre), "Falta este campo."))
    for nombre, v in valor.items():
        sub = f"{ruta}.{nombre}" if ruta else str(nombre)
        if nombre in props:
            salida += errores(v, props[nombre], sub, prof + 1)
        elif schema.get("additionalProperties") is False:
            salida.append((sub, "Campo desconocido."))
    return salida
