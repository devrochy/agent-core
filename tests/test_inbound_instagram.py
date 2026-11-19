"""Entrada de Instagram: ecos descartados y clientes separados por cuenta (ADR-0152)."""

from __future__ import annotations

from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound


def _payload(objeto: str, entry_id: str, *eventos: dict) -> dict:
    return {"object": objeto, "entry": [{"id": entry_id, "messaging": list(eventos)}]}


def _mensaje(sender: str, recipient: str, texto: str, mid: str, **extra) -> dict:
    return {"sender": {"id": sender}, "recipient": {"id": recipient}, "message": {"mid": mid, "text": texto, **extra}}


def test_un_eco_de_instagram_tampoco_se_procesa():
    payload = _payload("instagram", "IG1", _mensaje("IG1", "IGSID9", "respuesta", "m3", is_echo=True))
    assert WhatsAppMetaInbound().parse_inbound(payload) == []


def test_el_cliente_de_instagram_lleva_la_cuenta_en_su_clave():
    payload = _payload("instagram", "IG1", _mensaje("IGSID9", "IG1", "hola", "m4"))
    (mensaje,) = WhatsAppMetaInbound().parse_inbound(payload)
    assert mensaje.channel == "instagram"
    assert mensaje.customer_external_id == "instagram:IG1:IGSID9"
    assert mensaje.connection_external_id == "IG1"


def test_el_mismo_identificador_en_dos_cuentas_son_dos_clientes():
    uno = WhatsAppMetaInbound().parse_inbound(_payload("instagram", "IG1", _mensaje("IGSID9", "IG1", "hola", "a")))
    dos = WhatsAppMetaInbound().parse_inbound(_payload("instagram", "IG2", _mensaje("IGSID9", "IG2", "hola", "b")))
    assert uno[0].customer_external_id != dos[0].customer_external_id


def test_whatsapp_no_cambia():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "W", "changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "P1"}, "contacts": [{"wa_id": "573001112233", "profile": {"name": "Ana"}}],
            "messages": [{"id": "wamid.1", "from": "573001112233", "type": "text", "text": {"body": "hola"}}],
        }}]}],
    }
    (mensaje,) = WhatsAppMetaInbound().parse_inbound(payload)
    assert mensaje.customer_external_id == "whatsapp:+573001112233"
