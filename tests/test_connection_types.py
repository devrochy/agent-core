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
    setup_url,
    types_by_category,
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
    mensajeria = messaging_type_ids()
    assert "whatsapp_meta" in mensajeria
    assert "email_smtp" not in mensajeria
    assert "google_calendar" not in mensajeria


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


def test_types_by_category_agrupa_y_ordena():
    groups = types_by_category()
    labels = [g["label"] for g in groups]
    assert labels == ["Comunicación", "Correo", "Calendario"]
    # Los no disponibles también están (para "Próximamente").
    comm = next(g for g in groups if g["category"] == CATEGORY_COMMUNICATION)
    ids = [t.id for t in comm["items"]]
    assert "instagram_meta" in ids
    assert ids == sorted(ids, key=lambda i: get_connection_type(i).sort_order)


def test_setup_url_por_setup_kind_y_disponibilidad():
    email = get_connection_type("email_smtp")
    wa = get_connection_type("whatsapp_meta")
    cal = get_connection_type("google_calendar")
    off = get_connection_type("instagram_meta")
    assert setup_url(email) == "/admin/conexiones/nuevo?type=email_smtp"
    assert setup_url(wa) == "/admin/conexiones/wizard/whatsapp/elegir-entorno"
    assert setup_url(cal) == "/admin/plugins/pack_calendar/oauth"
    assert setup_url(off) == ""


def test_webchat_es_conversacional_y_no_aparece_en_el_selector():
    """El chat web es conexión (ADR-0115 §4) pero se configura en Configuración → Chat web."""
    webchat = get_connection_type("webchat")
    assert webchat is not None
    assert (webchat.category, webchat.channel, webchat.provider) == (CATEGORY_COMMUNICATION, "web", "web")
    assert webchat.produces_conversations is True
    assert webchat.external_id_key == ""  # la clave la genera el servidor, no el formulario
    en_selector = {t.id for grupo in types_by_category() for t in grupo["items"]}
    assert "webchat" not in en_selector and "whatsapp_meta" in en_selector


def test_messenger_esta_disponible_e_instagram_todavia_no():
    # Messenger tiene alta (HU-CHAN-01); Instagram no tiene adaptador ni alta (HU-CHAN-04).
    messenger = get_connection_type("messenger_meta")
    assert messenger is not None and messenger.available is True
    instagram = get_connection_type("instagram_meta")
    assert instagram is not None and instagram.available is False


def test_todo_canal_de_mensajeria_de_meta_disponible_tiene_adaptador_de_salida():
    """Un tipo disponible sin adaptador crearía conexiones que no pueden responder."""
    from core.gateway.channels.registry import CANALES_DE_MENSAJERIA_META, outbound_adapter_for

    for tipo in CONNECTION_TYPES:
        if tipo.available and tipo.provider == "meta" and tipo.channel in CANALES_DE_MENSAJERIA_META:
            assert outbound_adapter_for(tipo.provider, tipo.channel) is not None, tipo.id
