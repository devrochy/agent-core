"""Canal de correo saliente: asunto embebido y envío SMTP (ADR-0071)."""

from __future__ import annotations

from core.gateway.channels.email import _send_sync, single_recipient, split_subject
from core.gateway.channels.registry import outbound_adapter_for


def test_single_recipient_rechaza_listas_y_crlf():
    assert single_recipient("cliente@ejemplo.com") == "cliente@ejemplo.com"
    assert single_recipient("Nombre <cliente@ejemplo.com>") == "cliente@ejemplo.com"
    assert single_recipient("a@x.com, b@y.com") is None
    assert single_recipient("a@x.com\r\nBcc: b@y.com") is None
    assert single_recipient("no-es-email") is None
    assert single_recipient("") is None


def test_split_subject_con_y_sin_prefijo():
    assert split_subject("Subject: Hola\n\nCuerpo del correo") == ("Hola", "Cuerpo del correo")
    assert split_subject("sin asunto definido") == ("(sin asunto)", "sin asunto definido")


def test_registry_resuelve_el_adaptador_smtp():
    adapter = outbound_adapter_for("smtp")
    assert adapter.provider == "smtp"


def test_send_sync_usa_starttls_y_login(monkeypatch):
    from core.gateway.channels import email as mod

    calls: dict[str, object] = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=30):
            calls["host"] = host
            calls["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            calls["starttls"] = True

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, msg):
            calls["subject"] = msg["Subject"]
            calls["to"] = msg["To"]
            calls["from"] = msg["From"]

    monkeypatch.setattr(mod.smtplib, "SMTP", _FakeSMTP)

    message_id = _send_sync(
        {
            "host": "smtp.ejemplo.com",
            "port": "587",
            "username": "u",
            "password": "p",
            "from_address": "negocio@ejemplo.com",
            "use_tls": "true",
        },
        "cliente@ejemplo.com",
        "Asunto",
        "Cuerpo",
    )

    assert calls["host"] == "smtp.ejemplo.com"
    assert calls["port"] == 587
    assert calls["starttls"] is True
    assert calls["login"] == ("u", "p")
    assert calls["to"] == "cliente@ejemplo.com"
    assert calls["from"] == "negocio@ejemplo.com"
    assert calls["subject"] == "Asunto"
    assert message_id.startswith("<")
