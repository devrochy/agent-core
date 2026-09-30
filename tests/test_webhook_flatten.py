"""Aplanado de payloads de webhook de Meta (core/gateway/webhook_flatten.py)."""

from __future__ import annotations

from core.gateway.webhook_flatten import flatten_payload


def _whatsapp_payload(entries: list[dict]) -> dict:
    return {"object": "whatsapp_business_account", "entry": entries}


def test_itera_todas_las_entries_no_solo_la_primera():
    payload = _whatsapp_payload(
        [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "111"},
                            "messages": [{"id": "wamid.1"}],
                        },
                    }
                ],
            },
            {
                "id": "waba-2",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "222"},
                            "messages": [{"id": "wamid.2"}],
                        },
                    }
                ],
            },
            {
                "id": "waba-3",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "333"},
                            "messages": [{"id": "wamid.3"}],
                        },
                    }
                ],
            },
        ]
    )
    events = flatten_payload(payload)
    assert {e.entry_id for e in events} == {"waba-1", "waba-2", "waba-3"}
    assert {e.external_id for e in events} == {"wamid.1", "wamid.2", "wamid.3"}


def test_messages_y_statuses_comparten_field_pero_se_discriminan_por_clave():
    payload = _whatsapp_payload(
        [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "111"},
                            "messages": [{"id": "wamid.1"}],
                        },
                    },
                    {
                        "field": "messages",  # MISMO field, evento distinto
                        "value": {
                            "metadata": {"phone_number_id": "111"},
                            "statuses": [{"id": "wamid.1", "status": "delivered"}],
                        },
                    },
                ],
            }
        ]
    )
    events = flatten_payload(payload)
    assert len(events) == 2
    ids = {e.external_id for e in events}
    assert "wamid.1" in ids  # el mensaje
    assert "wamid.1:delivered" in ids  # el status, mismo wamid, evento distinto


def test_mismo_wamid_sent_delivered_read_son_tres_eventos():
    value = {"metadata": {"phone_number_id": "111"}, "statuses": [
        {"id": "wamid.1", "status": "sent"},
        {"id": "wamid.1", "status": "delivered"},
        {"id": "wamid.1", "status": "read"},
    ]}
    payload = _whatsapp_payload([{"id": "waba-1", "changes": [{"field": "messages", "value": value}]}])
    events = flatten_payload(payload)
    assert {e.external_id for e in events} == {"wamid.1:sent", "wamid.1:delivered", "wamid.1:read"}


def test_fallback_sha256_es_deterministico_entre_llamadas():
    value = {"metadata": {"phone_number_id": "111"}, "account_alerts": [{"type": "x"}]}
    payload = _whatsapp_payload([{"id": "waba-1", "changes": [{"field": "account_alerts", "value": value}]}])
    events1 = flatten_payload(payload)
    events2 = flatten_payload(payload)
    assert len(events1) == 1
    assert events1[0].external_id == events2[0].external_id
    assert events1[0].external_id != ""


def test_fallback_sha256_cambia_si_cambia_el_value():
    payload_a = _whatsapp_payload(
        [{"id": "waba-1", "changes": [{"field": "x", "value": {"a": 1}}]}]
    )
    payload_b = _whatsapp_payload(
        [{"id": "waba-1", "changes": [{"field": "x", "value": {"a": 2}}]}]
    )
    hash_a = flatten_payload(payload_a)[0].external_id
    hash_b = flatten_payload(payload_b)[0].external_id
    assert hash_a != hash_b


def test_messenger_messaging_directo_sin_changes():
    payload = {
        "object": "page",
        "entry": [
            {
                "id": "page-1",
                "messaging": [{"sender": {"id": "psid-1"}, "message": {"mid": "mid.1", "text": "hola"}}],
            }
        ],
    }
    events = flatten_payload(payload)
    assert len(events) == 1
    assert events[0].object_type == "page"
    assert events[0].field == "messaging"
    assert events[0].external_id == "mid.1"
    assert events[0].entry_id == "page-1"


def test_instagram_messaging():
    payload = {
        "object": "instagram",
        "entry": [
            {"id": "ig-1", "messaging": [{"sender": {"id": "igsid-1"}, "message": {"mid": "mid.ig.1"}}]}
        ],
    }
    events = flatten_payload(payload)
    assert len(events) == 1
    assert events[0].object_type == "instagram"
    assert events[0].entry_id == "ig-1"


def test_entry_sin_id_se_ignora():
    payload = _whatsapp_payload([{"changes": [{"field": "messages", "value": {}}]}])
    assert flatten_payload(payload) == []


def test_object_desconocido_no_rompe():
    payload = {"object": "algo_nuevo_de_meta", "entry": [{"id": "x", "changes": []}]}
    assert flatten_payload(payload) == []
