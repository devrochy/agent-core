"""FormGenerator: modelo Pydantic -> formulario Bootstrap 5.

Cada plugin declara su config como Pydantic (config_schema). El panel admin
genera el formulario automáticamente: cero HTML escrito por el autor del plugin.

Mapeo de tipos:
  SecretStr        -> <input type="password">   (cifrado Fernet al guardar)
  bool             -> <input type="checkbox">
  int / float      -> <input type="number">
  Literal[...]     -> <select>
  str (default)    -> <input type="text">
  Field(description=...) -> texto de ayuda bajo el campo
"""

from __future__ import annotations

import html
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, SecretStr
from pydantic.fields import FieldInfo


def render_form_fields(
    schema: type[BaseModel], current_values: dict[str, Any] | None = None
) -> str:
    values = current_values or {}
    parts: list[str] = []
    for name, field in schema.model_fields.items():
        parts.append(_render_field(name, field, values.get(name)))
    return "\n".join(parts)


def _render_field(name: str, field: FieldInfo, current: Any) -> str:
    label = html.escape(field.title or name.replace("_", " ").capitalize())
    help_text = html.escape(field.description or "")
    required = field.is_required()
    annotation = field.annotation
    default = current if current is not None else (field.default if field.default is not None else "")

    help_html = f'<div class="form-text">{help_text}</div>' if help_text else ""
    required_attr = "required" if required else ""

    # SecretStr -> password
    if annotation is SecretStr or (
        get_origin(annotation) is None and isinstance(default, SecretStr)
    ):
        placeholder = "••••••••" if current else ""
        return f"""
<div class="mb-3">
  <label class="form-label" for="{name}">{label}</label>
  <input type="password" class="form-control" id="{name}" name="{name}"
         placeholder="{placeholder}" {required_attr if not current else ""}>
  {help_html}
</div>"""

    # bool -> checkbox
    if annotation is bool:
        checked = "checked" if default in (True, "true", "on") else ""
        return f"""
<div class="mb-3 form-check">
  <input type="checkbox" class="form-check-input" id="{name}" name="{name}" {checked}>
  <label class="form-check-label" for="{name}">{label}</label>
  {help_html}
</div>"""

    # Literal -> select
    if get_origin(annotation) is Literal:
        options = "".join(
            f'<option value="{html.escape(str(opt))}" '
            f'{"selected" if str(default) == str(opt) else ""}>{html.escape(str(opt))}</option>'
            for opt in get_args(annotation)
        )
        return f"""
<div class="mb-3">
  <label class="form-label" for="{name}">{label}</label>
  <select class="form-select" id="{name}" name="{name}" {required_attr}>{options}</select>
  {help_html}
</div>"""

    # int / float -> number
    if annotation in (int, float):
        step = "1" if annotation is int else "any"
        return f"""
<div class="mb-3">
  <label class="form-label" for="{name}">{label}</label>
  <input type="number" step="{step}" class="form-control" id="{name}" name="{name}"
         value="{html.escape(str(default))}" {required_attr}>
  {help_html}
</div>"""

    # str (default)
    value = "" if isinstance(default, SecretStr) else html.escape(str(default))
    return f"""
<div class="mb-3">
  <label class="form-label" for="{name}">{label}</label>
  <input type="text" class="form-control" id="{name}" name="{name}"
         value="{value}" {required_attr}>
  {help_html}
</div>"""


def parse_form_values(
    schema: type[BaseModel], form_data: dict[str, str], previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Convierte los strings del form a los tipos del schema.

    Campos password vacíos conservan el valor previo (no se pisa el secreto).
    """
    previous = previous or {}
    values: dict[str, Any] = {}
    for name, field in schema.model_fields.items():
        annotation = field.annotation
        raw = form_data.get(name)

        if annotation is bool:
            values[name] = name in form_data and form_data[name] in ("on", "true", "1")
            continue
        if raw is None or raw == "":
            if annotation is SecretStr and name in previous:
                values[name] = previous[name]  # conservar secreto previo
            elif not field.is_required():
                if field.default is not None:
                    values[name] = (
                        field.default.get_secret_value()
                        if isinstance(field.default, SecretStr)
                        else field.default
                    )
            continue
        if annotation is int:
            values[name] = int(raw)
        elif annotation is float:
            values[name] = float(raw)
        else:
            values[name] = raw
    return values
