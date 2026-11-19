"""Coerción de valores JSON contra un JSON Schema (API `/api/v1`).

Los formularios del panel envían JSON nativo (no strings de un `<form>`), así que
`parse_json_schema_form` (que coacciona strings) no aplica. Este helper replica su
semántica: tipos del schema, defaults y **secretos vacíos que conservan el valor
previo**. Lo comparten la config de plugins y las credenciales de conexiones.
"""

from __future__ import annotations

from typing import Any


def is_secret(prop: dict[str, Any]) -> bool:
    return bool(prop.get("writeOnly")) or prop.get("format") == "password"


def is_hidden(prop: dict[str, Any]) -> bool:
    """Campo marcado `json_schema_extra={"oculto": True}`: no sale en el formulario ni se edita desde él."""
    return bool(prop.get("oculto"))


def hide_fields(schema: dict[str, Any], values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """El esquema y los valores sin los campos ocultos (lo que el formulario debe mostrar)."""
    props = schema.get("properties") or {}
    ocultos = {n for n, p in props.items() if isinstance(p, dict) and is_hidden(p)}
    if not ocultos:
        return schema, values
    visible = {**schema, "properties": {n: p for n, p in props.items() if n not in ocultos}}
    if isinstance(schema.get("required"), list):
        visible["required"] = [n for n in schema["required"] if n not in ocultos]
    return visible, {n: v for n, v in values.items() if n not in ocultos}


def coerce_schema_values(
    schema: dict[str, Any], values: dict[str, Any], previous: dict[str, Any]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, prop in (schema.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        if is_hidden(prop):
            # El formulario no lo muestra: lo guardado se conserva y lo que llegue se ignora.
            if name in previous:
                out[name] = previous[name]
            elif "default" in prop:
                out[name] = prop["default"]
            continue
        if name not in values:
            if is_secret(prop) and name in previous:
                out[name] = previous[name]
            elif "default" in prop:
                out[name] = prop["default"]
            continue
        raw = values[name]
        if is_secret(prop) and raw in (None, ""):
            if name in previous:
                out[name] = previous[name]
            continue
        ptype = prop.get("type")
        try:
            if ptype == "boolean":
                out[name] = bool(raw)
            elif ptype == "integer":
                out[name] = int(raw)
            elif ptype == "number":
                out[name] = float(raw)
            else:
                out[name] = raw
        except (TypeError, ValueError):
            continue
    return out


def redact_secrets(
    schema: dict[str, Any], values: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Quita los valores secretos y devuelve qué campos ya tienen valor."""
    out = dict(values)
    configured: list[str] = []
    for name, prop in (schema.get("properties") or {}).items():
        if isinstance(prop, dict) and is_secret(prop):
            if out.get(name) not in (None, "", "**********"):
                configured.append(name)
            out.pop(name, None)
    return out, configured
