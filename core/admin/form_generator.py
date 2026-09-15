"""FormEngine: JSON Schema -> formulario Bootstrap 5 (Fase D, Sprint 4).

Un solo motor para todo form del panel:
  - config de plugins (Pydantic -> model_json_schema(), delegación automática)
  - vistas por descriptor de los packs (register_view)
  - (futuro) editor de AgentSpec

Mapeo de tipos del JSON Schema:
  boolean            -> <input type="checkbox">
  integer / number   -> <input type="number"> (min/max)
  string + enum      -> <select>
  string + format:password (writeOnly) -> <input type="password"> (cifrado al guardar)
  string + format:textarea -> <textarea> (texto largo, ej. DSL multi-línea)
  string (default)   -> <input type="text">
  title / description -> label y texto de ayuda
"""

from __future__ import annotations

import html
from typing import Any

from pydantic import BaseModel


# ---------------------------------------------------------------- JSON Schema
def render_json_schema_form(schema: dict[str, Any], current_values: dict[str, Any] | None = None) -> str:
    """Renderiza un form a partir de un JSON Schema ({"type":"object", properties, required})."""
    values = current_values or {}
    properties = schema.get("properties", {}) or {}
    required = set(schema.get("required", []) or [])
    parts: list[str] = []
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        parts.append(_render_prop(name, prop, values.get(name), name in required))
    return "\n".join(parts)


def _render_prop(name: str, prop: dict[str, Any], current: Any, required: bool) -> str:
    label = html.escape(str(prop.get("title") or name.replace("_", " ").capitalize()))
    help_text = html.escape(str(prop.get("description") or ""))
    ptype = prop.get("type")
    enum = prop.get("enum")
    default = current if current is not None else prop.get("default")

    help_html = f'<div class="form-text">{help_text}</div>' if help_text else ""
    required_attr = "required" if required else ""

    # boolean -> checkbox
    if ptype == "boolean":
        checked = "checked" if default in (True, "true", "on") else ""
        return f"""
<div class="mb-3 form-check">
  <input type="checkbox" class="form-check-input" id="{name}" name="{name}" {checked}>
  <label class="form-check-label" for="{name}">{label}</label>
  {help_html}
</div>"""

    # string + enum -> select
    if ptype == "string" and enum:
        options = "".join(
            f'<option value="{html.escape(str(opt))}" '
            f'{"selected" if str(default) == str(opt) else ""}>{html.escape(str(opt))}</option>'
            for opt in enum
        )
        return f"""
<div class="mb-3">
  <label class="form-label" for="{name}">{label}</label>
  <select class="form-select" id="{name}" name="{name}" {required_attr}>{options}</select>
  {help_html}
</div>"""

    # secret / writeOnly -> password
    if prop.get("format") == "password" or prop.get("writeOnly"):
        placeholder = "••••••••" if current else ""
        return f"""
<div class="mb-3">
  <label class="form-label" for="{name}">{label}</label>
  <input type="password" class="form-control" id="{name}" name="{name}"
         placeholder="{placeholder}" {required_attr if not current else ""}>
  {help_html}
</div>"""

    # string + format=textarea -> <textarea> (texto largo, reusable para
    # cualquier campo futuro, no específico a un caso)
    if ptype == "string" and prop.get("format") == "textarea":
        value = "" if isinstance(default, dict) else html.escape(str(default or ""))
        return f"""
<div class="mb-3">
  <label class="form-label" for="{name}">{label}</label>
  <textarea class="form-control" id="{name}" name="{name}" rows="4" {required_attr}>{value}</textarea>
  {help_html}
</div>"""

    # number / integer
    if ptype in ("number", "integer"):
        step = "1" if ptype == "integer" else "any"
        min_attr = f' min="{prop["minimum"]}"' if "minimum" in prop else ""
        max_attr = f' max="{prop["maximum"]}"' if "maximum" in prop else ""
        return f"""
<div class="mb-3">
  <label class="form-label" for="{name}">{label}</label>
  <input type="number" step="{step}" class="form-control" id="{name}" name="{name}"
         value="{html.escape(str(default))}" {required_attr}{min_attr}{max_attr}>
  {help_html}
</div>"""

    # string (default)
    value = "" if isinstance(default, dict) else html.escape(str(default))
    return f"""
<div class="mb-3">
  <label class="form-label" for="{name}">{label}</label>
  <input type="text" class="form-control" id="{name}" name="{name}"
         value="{value}" {required_attr}>
  {help_html}
</div>"""


def parse_json_schema_form(
    schema: dict[str, Any],
    form_data: dict[str, str],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convierte los strings del form a los tipos del JSON Schema.

    Campos password vacíos conservan el valor previo (no se pisa el secreto).
    """
    previous = previous or {}
    properties = schema.get("properties", {}) or {}
    values: dict[str, Any] = {}
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        ptype = prop.get("type")
        raw = form_data.get(name)

        if ptype == "boolean":
            values[name] = name in form_data and form_data[name] in ("on", "true", "1")
            continue
        if raw is None or raw == "":
            if prop.get("format") == "password" and name in previous:
                values[name] = previous[name]
            elif "default" in prop:
                values[name] = prop["default"]
            continue
        if ptype == "integer":
            try:
                values[name] = int(raw)
            except ValueError:
                continue
        elif ptype == "number":
            try:
                values[name] = float(raw)
            except ValueError:
                continue
        else:
            values[name] = raw
    return values


# ---------------------------------------------------------------- Pydantic (legacy)
def render_form_fields(
    schema: type[BaseModel], current_values: dict[str, Any] | None = None
) -> str:
    """Form de config de un plugin: delega al motor JSON Schema (model_json_schema)."""
    return render_json_schema_form(schema.model_json_schema(), current_values)


def parse_form_values(
    schema: type[BaseModel], form_data: dict[str, str], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Parseo de config de un plugin: delega al motor JSON Schema."""
    return parse_json_schema_form(schema.model_json_schema(), form_data, previous)