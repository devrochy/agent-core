"""`validate_template_components`: BODY obligatorio, nombres exactos de
`example`, `param_name` <-> `{{...}}` en NAMED, botones no soportados."""

from __future__ import annotations

import pytest

from core.gateway.meta_templates import TemplateValidationError, validate_template_components


def test_body_faltante_falla():
    with pytest.raises(TemplateValidationError, match="BODY es obligatorio"):
        validate_template_components("UTILITY", [{"type": "FOOTER", "text": "x"}], None)


def test_body_text_named_params_no_matchea_placeholder_falla():
    components = [
        {
            "type": "BODY", "text": "Hola {{nombre}}",
            "example": {"body_text_named_params": [{"param_name": "otro", "example": "Juan"}]},
        }
    ]
    with pytest.raises(TemplateValidationError, match="nombre"):
        validate_template_components("UTILITY", components, "NAMED")


def test_body_text_named_params_con_param_name_sobrante_falla():
    components = [
        {
            "type": "BODY", "text": "Hola sin parametros",
            "example": {"body_text_named_params": [{"param_name": "nombre", "example": "Juan"}]},
        }
    ]
    with pytest.raises(TemplateValidationError, match="nombre"):
        validate_template_components("UTILITY", components, "NAMED")


def test_body_positional_named_correctos_pasa():
    components = [
        {
            "type": "BODY", "text": "Hola {{nombre}}, tu pedido {{pedido}}",
            "example": {
                "body_text_named_params": [
                    {"param_name": "nombre", "example": "Juan"},
                    {"param_name": "pedido", "example": "#123"},
                ]
            },
        }
    ]
    validate_template_components("UTILITY", components, "NAMED")


def test_body_text_positional_debe_ser_array_de_arrays():
    components = [{"type": "BODY", "text": "Hola {{1}}", "example": {"body_text": ["Juan"]}}]
    with pytest.raises(TemplateValidationError, match=r"(?i)array"):
        validate_template_components("UTILITY", components, None)


def test_body_text_positional_array_de_arrays_pasa():
    components = [{"type": "BODY", "text": "Hola {{1}}", "example": {"body_text": [["Juan"]]}}]
    validate_template_components("UTILITY", components, None)


def test_footer_con_example_falla():
    components = [
        {"type": "BODY", "text": "cuerpo"},
        {"type": "FOOTER", "text": "pie", "example": {"body_text": [["x"]]}},
    ]
    with pytest.raises(TemplateValidationError, match="FOOTER"):
        validate_template_components("UTILITY", components, None)


def test_header_media_sin_handle_falla():
    components = [
        {"type": "HEADER", "format": "IMAGE"},
        {"type": "BODY", "text": "cuerpo"},
    ]
    with pytest.raises(TemplateValidationError, match="header_handle"):
        validate_template_components("UTILITY", components, None)


def test_header_media_con_handle_pasa():
    components = [
        {"type": "HEADER", "format": "IMAGE", "example": {"header_handle": ["h123"]}},
        {"type": "BODY", "text": "cuerpo"},
    ]
    validate_template_components("UTILITY", components, None)


@pytest.mark.parametrize("button_type", ["FLOW", "OTP"])
def test_boton_no_soportado_falla(button_type):
    components = [
        {"type": "BODY", "text": "cuerpo"},
        {"type": "BUTTONS", "buttons": [{"type": button_type, "text": "x"}]},
    ]
    with pytest.raises(TemplateValidationError, match="no soportado"):
        validate_template_components("UTILITY", components, None)


@pytest.mark.parametrize("button_type", ["QUICK_REPLY", "URL", "PHONE_NUMBER", "COPY_CODE"])
def test_botones_soportados_pasan(button_type):
    button = {"type": button_type, "text": "x"}
    if button_type == "URL":
        button["url"] = "https://example.test"
    if button_type == "PHONE_NUMBER":
        button["phone_number"] = "+541100000000"
    if button_type == "COPY_CODE":
        button["example"] = "ABC123"
    components = [{"type": "BODY", "text": "cuerpo"}, {"type": "BUTTONS", "buttons": [button]}]
    validate_template_components("UTILITY", components, None)


def test_categoria_invalida_falla():
    with pytest.raises(TemplateValidationError, match="category"):
        validate_template_components("INVALIDA", [{"type": "BODY", "text": "x"}], None)
