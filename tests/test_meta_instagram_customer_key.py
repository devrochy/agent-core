"""El IGSID es único SOLO por cuenta profesional -- `customer_external_id`
debe incluir el `ig_user_id`, no solo el IGSID, para no cruzar clientes
entre dos cuentas de Instagram del mismo tenant."""

from __future__ import annotations

from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound


def _ig_payload(ig_user_id: str, igsid: str, mid: str, text: str) -> dict:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": ig_user_id,
                "messaging": [{"sender": {"id": igsid}, "message": {"mid": mid, "text": text}}],
            }
        ],
    }


def test_customer_external_id_incluye_ig_user_id():
    messages = WhatsAppMetaInbound().parse_inbound(_ig_payload("ig-account-1", "igsid-shared", "mid.1", "hola"))
    assert len(messages) == 1
    assert messages[0].customer_external_id == "instagram:ig-account-1:igsid-shared"


def test_mismo_igsid_en_dos_cuentas_no_colisiona():
    """El mismo IGSID (mismo usuario final) frente a DOS cuentas de
    Instagram distintas del mismo tenant debe producir dos claves
    DISTINTAS -- son clientes lógicamente separados."""
    msgs_a = WhatsAppMetaInbound().parse_inbound(_ig_payload("ig-account-A", "igsid-shared", "mid.a", "hola desde A"))
    msgs_b = WhatsAppMetaInbound().parse_inbound(_ig_payload("ig-account-B", "igsid-shared", "mid.b", "hola desde B"))
    assert msgs_a[0].customer_external_id != msgs_b[0].customer_external_id


def test_messenger_no_se_toca_por_este_fix():
    """Messenger sigue con el formato viejo -- no es parte de este ticket
    (deuda relacionada, documentada, no corregida acá)."""
    payload = {
        "object": "page",
        "entry": [{"id": "page-1", "messaging": [{"sender": {"id": "psid-1"}, "message": {"mid": "mid.m", "text": "hola"}}]}],
    }
    messages = WhatsAppMetaInbound().parse_inbound(payload)
    assert messages[0].customer_external_id == "messenger:psid-1"
