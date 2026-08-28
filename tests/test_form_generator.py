"""FormEngine por JSON Schema (Fase D, Sprint 4)."""

from __future__ import annotations

from core.admin.form_generator import (
    parse_json_schema_form,
    render_json_schema_form,
)


def test_render_json_schema_tipos() -> None:
    schema = {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": 0, "maximum": 10, "title": "Score"},
            "status": {"type": "string", "enum": ["nuevo", "calificado"]},
            "flag": {"type": "boolean", "default": True},
            "secret": {"type": "string", "format": "password", "writeOnly": True},
            "nota": {"type": "string", "description": "Ayuda"},
        },
        "required": ["status"],
    }
    html = render_json_schema_form(schema, {"score": 7})
    assert 'min="0"' in html and 'max="10"' in html
    assert "<select" in html and "nuevo" in html
    assert 'type="checkbox"' in html
    assert 'type="password"' in html
    assert "Ayuda" in html
    assert 'required' in html


def test_parse_json_schema_tipos_y_secretos() -> None:
    schema = {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "default": 3},
            "precio": {"type": "number"},
            "flag": {"type": "boolean", "default": False},
            "secret": {"type": "string", "format": "password"},
            "status": {"type": "string"},
        },
    }
    # parseo de tipos
    values = parse_json_schema_form(
        schema, {"score": "7", "precio": "12.5", "flag": "on", "status": "nuevo"}
    )
    assert values == {"score": 7, "precio": 12.5, "flag": True, "status": "nuevo"}

    # password vacío conserva el previo; integer vacío usa default
    prev = {"secret": "S3CRETO"}
    values2 = parse_json_schema_form(schema, {"secret": "", "score": ""}, prev)
    assert values2["secret"] == "S3CRETO"
    assert values2["score"] == 3


def test_delegacion_pydantic_usa_el_mismo_motor() -> None:
    from pack_calendar.config import CalendarConfig

    from core.admin.form_generator import parse_form_values, render_form_fields

    html = render_form_fields(CalendarConfig)
    assert 'type="password"' in html, "SecretStr debe renderizarse como password"

    prev = {"client_secret": "OLD"}
    parsed = parse_form_values(CalendarConfig, {"client_secret": ""}, prev)
    assert parsed.get("client_secret") == "OLD", "secreto previo preservado"