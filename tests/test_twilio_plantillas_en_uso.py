"""PR-7a de WhatsApp vía Twilio (ADR-0186 §8): plantillas de Twilio en el chat y en los avisos, sin base.

- El mensaje encolado lleva el nombre legible en `template_name` y el `ContentSid` con sus variables en un único
  componente `twilio_content`; el outbox lo manda por `send_content_template`.
- Una plantilla que el canal no sabe enviar es PERMANENTE (antes se reintentaba como error de red), en Meta y en
  Twilio.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from test_outbound_send_task import _send_one_fixture

from core.db.models import TwilioContentTemplate
from core.gateway.twilio import plantillas_en_uso as tpl

HX = "HX" + "a" * 32


def _fila(body="Hola {{1}}, tu cita es el {{2}}") -> TwilioContentTemplate:
    return TwilioContentTemplate(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        channel_connection_id=uuid.uuid4(),
        content_sid=HX,
        friendly_name="recordatorio",
        whatsapp_name="recordatorio",
        language="es",
        category="UTILITY",
        body=body,
        approval_status="approved",
    )


def test_placeholders_y_vista_previa():
    fila = _fila()
    assert tpl.placeholders(fila.body) == ["1", "2"]
    assert tpl.placeholders("{{2}} y {{ 1 }} y {{1}}") == ["1", "2"]
    assert tpl.vista_previa(fila, {"1": "Ana", "2": "lunes"}) == "Hola Ana, tu cita es el lunes"


def test_las_variables_se_limpian_como_en_meta():
    fila = _fila()
    sucio = {"1": "Ana\n\nMaría   López", "2": "x" * 5000}
    [comp] = tpl.componentes(fila, sucio)
    assert "\n" not in comp["variables"]["1"] and "   " not in comp["variables"]["1"]
    assert len(comp["variables"]["2"]) < 5000
    assert (
        tpl.vista_previa(fila, sucio)
        == f"Hola {comp['variables']['1']}, tu cita es el {comp['variables']['2']}"
    )


def test_componentes_y_su_lectura():
    fila = _fila()
    comps = tpl.componentes(fila, {"1": "Ana", "2": "lunes", "3": "sobra"})
    assert comps == [
        {"type": "twilio_content", "content_sid": HX, "variables": {"1": "Ana", "2": "lunes"}}
    ]
    assert tpl.contenido_de(comps) == (HX, {"1": "Ana", "2": "lunes"})


@pytest.mark.parametrize(
    "malos",
    [
        None,
        [],
        [{"type": "body", "parameters": []}],
        [{"type": "twilio_content", "content_sid": "HXcorto"}],
        [
            {"type": "twilio_content", "content_sid": HX},
            {"type": "twilio_content", "content_sid": HX},
        ],
    ],
)
def test_contenido_de_rechaza(malos):
    assert tpl.contenido_de(malos) is None


async def test_es_de_twilio_sin_conexion():
    assert await tpl.es_de_twilio(SimpleNamespace(connection_id=None, channel="whatsapp")) is False


def _fixture_twilio(monkeypatch, adapter):
    import core.tasks.outbound_send as mod

    msg, tenant_id, calls = _send_one_fixture(monkeypatch, adapter=adapter)

    async def _creds(*a, **k):
        return (
            "twilio",
            {"account_sid": "AC" + "1" * 32, "auth_token": "t", "from": "whatsapp:+15550001111"},
        )

    monkeypatch.setattr(mod, "resolve_credentials", _creds)
    return msg, tenant_id, calls


async def test_outbox_manda_la_plantilla_de_twilio_por_content_sid(monkeypatch):
    from core.tasks.outbound_send import _send_one

    enviados: list = []

    async def _content(credentials, to, content_sid, variables):
        enviados.append((to, content_sid, variables))
        return "SM" + "9" * 32

    async def _nunca(*a, **k):
        raise AssertionError("no debe salir por send_template ni como texto")

    adapter = SimpleNamespace(
        send_text=_nunca, send_image=_nunca, send_template=_nunca, send_content_template=_content
    )
    msg, tenant_id, _ = _fixture_twilio(monkeypatch, adapter)
    msg.template_name = "recordatorio"
    msg.template_language = "es"
    msg.template_components = tpl.componentes(_fila(), {"1": "Ana", "2": "lunes"})
    assert await _send_one(msg.id, tenant_id) is None
    assert msg.status == "sent"
    assert enviados == [("whatsapp:+573001234567", HX, {"1": "Ana", "2": "lunes"})]


async def test_plantilla_de_twilio_mal_formada_es_permanente(monkeypatch):
    from core.tasks.outbound_send import _send_one

    async def _nunca(*a, **k):
        raise AssertionError("no debe enviarse")

    adapter = SimpleNamespace(send_text=_nunca, send_image=_nunca, send_content_template=_nunca)
    msg, tenant_id, calls = _fixture_twilio(monkeypatch, adapter)
    msg.template_name = "recordatorio"
    msg.template_components = [
        {"type": "body", "parameters": []}
    ]  # de Meta, en una conversación de Twilio
    error = await _send_one(msg.id, tenant_id)
    assert error is not None and error.category == "PERMANENT"
    assert calls["release"] == 1


async def test_canal_sin_plantillas_es_permanente_tambien_en_meta(monkeypatch):
    """Antes el `ChannelError` de un canal sin plantillas caía en el `except Exception` y se reintentaba."""
    from core.shared.exceptions import ChannelError
    from core.tasks.outbound_send import _send_one

    async def _sin_plantillas(*a, **k):
        raise ChannelError("El canal x no envía plantillas")

    async def _nunca(*a, **k):
        raise AssertionError("no debe enviarse")

    adapter = SimpleNamespace(send_text=_nunca, send_image=_nunca, send_template=_sin_plantillas)
    msg, tenant_id, _ = _send_one_fixture(monkeypatch, adapter=adapter)
    msg.template_name = "aviso"
    msg.template_components = []
    error = await _send_one(msg.id, tenant_id)
    assert error is not None and error.category == "PERMANENT"
