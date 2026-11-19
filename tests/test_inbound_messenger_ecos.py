"""Un eco de Messenger no es un mensaje del cliente (ADR-0152): la Página se suscribe a `message_echoes`."""

from __future__ import annotations

from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound


def _mensaje(sender: str, recipient: str, texto: str, mid: str, **extra) -> dict:
    return {"sender": {"id": sender}, "recipient": {"id": recipient}, "message": {"mid": mid, "text": texto, **extra}}


def test_un_eco_de_la_pagina_no_es_un_mensaje_del_cliente():
    # Meta devuelve al webhook lo que la propia Página envió: el remitente es la Página.
    payload = {
        "object": "page",
        "entry": [{"id": "PAG1", "messaging": [
            _mensaje("CLIENTE", "PAG1", "hola", "m1"),
            _mensaje("PAG1", "CLIENTE", "Hola, ¿en qué te ayudo?", "m2", is_echo=True),
        ]}],
    }
    mensajes = WhatsAppMetaInbound().parse_inbound(payload)
    assert [m.external_message_id for m in mensajes] == ["m1"]
    assert mensajes[0].customer_external_id == "messenger:CLIENTE"
