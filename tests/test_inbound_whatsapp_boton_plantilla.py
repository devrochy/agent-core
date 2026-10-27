"""Un quick reply de plantilla de Meta llega como `type: "button"` y no se descarta."""

from __future__ import annotations

from typing import Any

from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound


def _payload(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "WABA1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "573001112233", "phone_number_id": "PNID1"},
                    "contacts": [{"profile": {"name": "Ana"}, "wa_id": "573009998877"}],
                    "messages": [{"from": "573009998877", "id": "wamid.BTN1", "timestamp": "1761500000", **msg}],
                },
            }],
        }],
    }


def _parse(msg: dict[str, Any]):
    return WhatsAppMetaInbound().parse_inbound(_payload(msg))


def test_boton_de_plantilla_llega_con_texto_y_payload():
    # Forma real que entrega Meta al tocar un quick reply de una plantilla.
    mensajes = _parse({
        "type": "button",
        "button": {"payload": "agenda:confirmar:123", "text": "Confirmar cita"},
        "context": {"from": "573001112233", "id": "wamid.PLANTILLA"},
    })
    assert len(mensajes) == 1
    m = mensajes[0]
    assert m.content == "Confirmar cita"
    assert m.interactive_id == "agenda:confirmar:123"
    assert m.external_message_id == "wamid.BTN1"
    assert m.customer_external_id == "whatsapp:+573009998877"
    assert m.customer_name == "Ana"
    assert m.connection_external_id == "PNID1"
    assert m.media == []


def test_boton_sin_payload_conserva_el_texto():
    m = _parse({"type": "button", "button": {"text": "Más información"}})[0]
    assert m.content == "Más información"
    assert m.interactive_id == ""


def test_boton_con_texto_vacio_conserva_el_payload():
    m = _parse({"type": "button", "button": {"payload": "opt:1", "text": ""}})[0]
    assert m.content == ""
    assert m.interactive_id == "opt:1"


def test_boton_con_payload_nulo_o_sin_objeto_no_rompe():
    assert _parse({"type": "button", "button": {"payload": None, "text": "Sí"}})[0].interactive_id == ""
    m = _parse({"type": "button"})[0]
    assert (m.content, m.interactive_id) == ("", "")


def test_payload_mayor_de_256_se_recorta():
    m = _parse({"type": "button", "button": {"payload": "x" * 400, "text": "Ok"}})[0]
    assert m.interactive_id == "x" * 256


def test_el_boton_interactivo_de_siempre_no_cambia():
    m = _parse({"type": "interactive", "interactive": {"type": "button_reply",
                                                         "button_reply": {"id": "b1", "title": "Sí"}}})[0]
    assert (m.content, m.interactive_id) == ("Sí", "b1")


def test_tipos_no_soportados_siguen_descartandose():
    assert _parse({"type": "sticker", "sticker": {"id": "s1"}}) == []
