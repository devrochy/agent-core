"""Catálogo de tipos de conexión y normalización de credenciales (ADR-0069)."""

from __future__ import annotations

from core.gateway.connection_types import (
    CATEGORY_CALENDAR,
    CATEGORY_COMMUNICATION,
    CATEGORY_EMAIL,
    CONNECTION_TYPES,
    external_id_for,
    get_connection_type,
    messaging_type_ids,
    normalize_credentials,
)


def test_catalogo_ids_unicos_y_clasificacion():
    ids = [t.id for t in CONNECTION_TYPES]
    assert len(ids) == len(set(ids))

    whatsapp = get_connection_type("whatsapp_meta")
    assert whatsapp is not None
    assert whatsapp.category == CATEGORY_COMMUNICATION
    assert whatsapp.available is True
    assert whatsapp.produces_conversations is True

    email = get_connection_type("email_smtp")
    assert email is not None
    assert email.category == CATEGORY_EMAIL
    assert email.available is True

    calendar = get_connection_type("google_calendar")
    assert calendar is not None
    assert calendar.category == CATEGORY_CALENDAR
    # No produce conversaciones: es una capacidad, no un canal de mensajería.
    assert calendar.produces_conversations is False


def test_solo_mensajeria_puede_ser_primaria():
    assert messaging_type_ids() == {"whatsapp_meta", "instagram_meta", "messenger_meta"}
    assert "email_smtp" not in messaging_type_ids()
    assert "google_calendar" not in messaging_type_ids()


def test_normalize_descarta_claves_no_declaradas_y_tipa():
    email = get_connection_type("email_smtp")
    out = normalize_credentials(
        email,
        {
            "host": "smtp.ejemplo.com",
            "port": "465",
            "use_tls": "false",
            "username": "u",
            "password": "p",
            "from_address": "a@b.com",
            "clave_intrusa": "no debe guardarse",
        },
    )
    assert out["port"] == 465
    assert out["use_tls"] is False
    assert "clave_intrusa" not in out


def test_normalize_conserva_secreto_previo_si_viene_vacio():
    email = get_connection_type("email_smtp")
    out = normalize_credentials(
        email,
        {"password": "", "host": "smtp.ejemplo.com"},
        previous={"password": "secreto-previo"},
    )
    assert out["password"] == "secreto-previo"
    assert out["host"] == "smtp.ejemplo.com"


def test_external_id_por_tipo():
    wa = get_connection_type("whatsapp_meta")
    email = get_connection_type("email_smtp")
    cal = get_connection_type("google_calendar")
    assert external_id_for(wa, {"phone_number_id": "123"}) == "123"
    assert external_id_for(email, {"from_address": "a@b.com"}) == "a@b.com"
    assert external_id_for(cal, {"account_email": "c@d.com"}) == "c@d.com"
