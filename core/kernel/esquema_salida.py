"""Subconjunto de JSON Schema que valida el kernel (adenda HU-AOS-19 de ADR-0113). Puro.

La salida de una consulta entre agentes (`ExposedQuery.outputSchema`) se valida aquí antes de entrar
como dato. Solo se aceptan estas palabras clave; el linter rechaza un schema que use otras
(fail-closed, sin una librería nueva):

`type` (`object`, `array`, `string`, `integer`, `number`, `boolean`, `null` o una lista de ellos),
`properties`, `required`, `additionalProperties` (booleano), `items`, `enum`, `maxLength`,
`maxItems` y `description`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SOPORTADAS = frozenset(
    {"type", "properties", "required", "additionalProperties", "items", "enum", "maxLength", "maxItems", "description"}
)
_TIPOS = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_PROFUNDIDAD = 6
_MAX_ERRORES = 10


def no_soportadas(schema: Any, ruta: str = "$", nivel: int = 0) -> list[str]:
    """Las palabras clave o formas que el kernel no valida (vacío = schema aceptable)."""
    if not isinstance(schema, Mapping):
        return [f"{ruta}: no es un objeto"]
    if nivel > _PROFUNDIDAD:
        return [f"{ruta}: demasiado profundo"]
    problemas = [f"{ruta}.{k}" for k in sorted(set(schema) - SOPORTADAS)]
    tipo = schema.get("type")
    tipos = tipo if isinstance(tipo, list) else [tipo] if tipo is not None else []
    if any(t not in _TIPOS for t in tipos):
        problemas.append(f"{ruta}.type")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        problemas.append(f"{ruta}.additionalProperties")
    propiedades = schema.get("properties")
    if propiedades is not None:
        if not isinstance(propiedades, Mapping):
            problemas.append(f"{ruta}.properties")
        else:
            for nombre, sub in propiedades.items():
                problemas.extend(no_soportadas(sub, f"{ruta}.properties.{str(nombre)[:40]}", nivel + 1))
    if "items" in schema:
        problemas.extend(no_soportadas(schema["items"], f"{ruta}.items", nivel + 1))
    if "required" in schema and not (
        isinstance(schema["required"], list) and all(isinstance(r, str) for r in schema["required"])
    ):
        problemas.append(f"{ruta}.required")
    if "enum" in schema and not isinstance(schema["enum"], list):
        problemas.append(f"{ruta}.enum")
    for clave in ("maxLength", "maxItems"):
        if clave in schema and not (isinstance(schema[clave], int) and not isinstance(schema[clave], bool) and schema[clave] >= 0):
            problemas.append(f"{ruta}.{clave}")
    return problemas


def _es(valor: Any, tipo: str) -> bool:
    if tipo == "object":
        return isinstance(valor, Mapping)
    if tipo == "array":
        return isinstance(valor, list)
    if tipo == "string":
        return isinstance(valor, str)
    if tipo == "boolean":
        return isinstance(valor, bool)
    if tipo == "integer":
        return isinstance(valor, int) and not isinstance(valor, bool)
    if tipo == "number":
        return isinstance(valor, int | float) and not isinstance(valor, bool)
    return valor is None


def errores(valor: Any, schema: Mapping[str, Any], ruta: str = "$") -> list[str]:
    """Rutas donde `valor` no cumple `schema` (como mucho 10; vacío = válido). Un schema con
    palabras no soportadas no valida nada: se rechaza entero (fail-closed)."""
    if no_soportadas(schema):
        return [f"{ruta}: schema no soportado"]
    salida: list[str] = []
    _validar(valor, schema, ruta, salida)
    return salida[:_MAX_ERRORES]


def _validar(valor: Any, schema: Mapping[str, Any], ruta: str, salida: list[str]) -> None:
    if len(salida) >= _MAX_ERRORES:
        return
    tipo = schema.get("type")
    tipos = tipo if isinstance(tipo, list) else [tipo] if tipo is not None else []
    if tipos and not any(_es(valor, t) for t in tipos):
        salida.append(f"{ruta}: tipo")
        return
    if "enum" in schema and valor not in schema["enum"]:
        salida.append(f"{ruta}: enum")
    if isinstance(valor, str) and "maxLength" in schema and len(valor) > schema["maxLength"]:
        salida.append(f"{ruta}: maxLength")
    if isinstance(valor, list):
        if "maxItems" in schema and len(valor) > schema["maxItems"]:
            salida.append(f"{ruta}: maxItems")
        if isinstance(schema.get("items"), Mapping):
            for i, item in enumerate(valor[:200]):
                _validar(item, schema["items"], f"{ruta}[{i}]", salida)
    if isinstance(valor, Mapping):
        propiedades = schema.get("properties") or {}
        for requerido in schema.get("required") or []:
            if requerido not in valor:
                salida.append(f"{ruta}.{requerido[:40]}: requerido")
        if schema.get("additionalProperties") is False:
            for extra in sorted(set(map(str, valor)) - set(propiedades))[:5]:
                salida.append(f"{ruta}.{extra[:40]}: no permitido")
        for nombre, sub in propiedades.items():
            if nombre in valor and isinstance(sub, Mapping):
                _validar(valor[nombre], sub, f"{ruta}.{nombre}", salida)


__all__ = ["SOPORTADAS", "errores", "no_soportadas"]
