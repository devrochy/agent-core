"""Errores de validación de Pydantic en español, por campo.

Lo comparten la API `/api/v1` (handler de `RequestValidationError`) y la configuración de
los packs (`plugin_manager.save_config`): el formulario del panel muestra el mensaje junto
al control (HU-FWK-08), así que tiene que ser texto para una persona, no el `msg` en inglés
de Pydantic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

MENSAJE_CAMPOS = "Revisa los campos marcados."

_POR_TIPO = {
    "missing": "Este campo es obligatorio.",
    "int_parsing": "Debe ser un número entero.",
    "int_type": "Debe ser un número entero.",
    "int_from_float": "Debe ser un número entero, sin decimales.",
    "float_parsing": "Debe ser un número.",
    "float_type": "Debe ser un número.",
    "decimal_parsing": "Debe ser un número.",
    "bool_parsing": "Debe ser verdadero o falso.",
    "bool_type": "Debe ser verdadero o falso.",
    "string_type": "Debe ser texto.",
    "list_type": "Debe ser una lista.",
    "dict_type": "Debe ser un objeto.",
    "uuid_parsing": "No es un identificador válido.",
    "date_from_datetime_parsing": "No es una fecha válida.",
    "date_parsing": "No es una fecha válida.",
    "datetime_parsing": "No es una fecha válida.",
    "json_invalid": "No es un JSON válido.",
    "extra_forbidden": "Este campo no se admite.",
    "finite_number": "Debe ser un número finito.",
}


def _numero(valor: Any) -> str:
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor)


def mensaje_es(error: Mapping[str, Any]) -> str:
    """Mensaje en español para un elemento de `ValidationError.errors()`."""
    tipo = str(error.get("type") or "")
    ctx = error.get("ctx") or {}
    if tipo == "greater_than_equal":
        return f"Debe ser mayor o igual que {_numero(ctx.get('ge'))}."
    if tipo == "greater_than":
        return f"Debe ser mayor que {_numero(ctx.get('gt'))}."
    if tipo == "less_than_equal":
        return f"Debe ser menor o igual que {_numero(ctx.get('le'))}."
    if tipo == "less_than":
        return f"Debe ser menor que {_numero(ctx.get('lt'))}."
    if tipo == "multiple_of":
        return f"Debe ser múltiplo de {_numero(ctx.get('multiple_of'))}."
    if tipo == "string_too_long":
        return f"Máximo {ctx.get('max_length')} caracteres."
    if tipo == "string_too_short":
        return f"Mínimo {ctx.get('min_length')} caracteres."
    if tipo == "too_long":
        return f"Máximo {ctx.get('max_length')} elementos."
    if tipo == "too_short":
        return f"Mínimo {ctx.get('min_length')} elementos."
    if tipo in ("literal_error", "enum"):
        esperado = ctx.get("expected")
        return f"Valor no admitido (se espera {esperado})." if esperado else "Valor no admitido."
    if tipo == "string_pattern_mismatch":
        return "El formato no es válido."
    if tipo in ("value_error", "assertion_error"):
        # Validadores propios: `raise ValueError("...")` ya trae el texto para la persona.
        texto = str(error.get("msg") or "")
        for prefijo in ("Value error, ", "Assertion failed, "):
            if texto.startswith(prefijo):
                texto = texto[len(prefijo):]
        return texto or "Valor no válido."
    if tipo in _POR_TIPO:
        return _POR_TIPO[tipo]
    return str(error.get("msg") or "Valor no válido.")


def campo_de(loc: Iterable[Any]) -> str:
    """`loc` de Pydantic/FastAPI → nombre del campo (`body` y `query` no son campos)."""
    partes = [str(p) for p in loc]
    if partes and partes[0] in ("body", "query", "path", "header", "cookie"):
        partes = partes[1:]
    return ".".join(partes)


def campos_de(errors: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """`{campo: mensaje}` para mostrar junto a cada control (primer error por campo)."""
    fields: dict[str, str] = {}
    for err in errors:
        campo = campo_de(err.get("loc") or ()) or "_"
        fields.setdefault(campo, mensaje_es(err))
    return fields
