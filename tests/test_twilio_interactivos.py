"""PR-7i de WhatsApp vía Twilio (ADR-0186 §9): botones y listas como Content de Twilio, sin base.

- Un Content por FORMA (`quick_reply:N`, `list:N`) con el cuerpo, los títulos y los ids en variables.
- El outbox manda el interactivo de una conversación de Twilio por su Content; un payload que no sirve es
  PERMANENTE y no cae a texto.
- Un Content borrado en la consola (404) se recrea una sola vez; otro error no.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from test_outbound_send_task import _send_one_fixture

from core.gateway.channels.base import ProviderError
from core.gateway.twilio import interactivos as it
from core.shared.exceptions import ChannelError

AC = "AC" + "1" * 32
HX1 = "HX" + "a" * 32
HX2 = "HX" + "b" * 32
CREDS = {"account_sid": AC, "auth_token": "t", "from": "whatsapp:+15550001111"}
BOTONES = {
    "type": "button",
    "body": "¿Qué día te sirve?",
    "buttons": [{"id": "opt:a", "title": "Lunes"}, {"id": "opt:b", "title": "Martes"}],
    "footer": "Responde con un botón",
}
LISTA = {
    "type": "list",
    "body": "Elige un servicio",
    "button": "Ver servicios",
    "rows": [
        {"id": "s1", "title": "Corte", "description": "30 min"},
        {"id": "s2", "title": "Tinte"},
    ],
}


def test_botones_por_forma_con_todo_en_variables():
    forma, tipos, variables = it.forma_y_variables(BOTONES)
    assert forma == "quick_reply:2"
    assert tipos == {
        "twilio/quick-reply": {
            "body": "{{1}}",
            "actions": [
                {"type": "QUICK_REPLY", "title": "{{2}}", "id": "{{3}}"},
                {"type": "QUICK_REPLY", "title": "{{4}}", "id": "{{5}}"},
            ],
        }
    }
    assert variables == {
        "1": "¿Qué día te sirve?\n\nResponde con un botón",
        "2": "Lunes",
        "3": "opt:a",
        "4": "Martes",
        "5": "opt:b",
    }


def test_lista_por_forma_y_descripcion_obligatoria():
    forma, tipos, variables = it.forma_y_variables(LISTA)
    assert forma == "list:2"
    picker = tipos["twilio/list-picker"]
    assert picker["body"] == "{{1}}" and picker["button"] == "{{2}}"
    assert picker["items"] == [
        {"item": "{{3}}", "id": "{{4}}", "description": "{{5}}"},
        {"item": "{{6}}", "id": "{{7}}", "description": "{{8}}"},
    ]
    assert variables["5"] == "30 min"
    assert variables["8"] == "-"  # Twilio exige descripción en cada fila


def test_dos_mensajes_de_la_misma_forma_comparten_el_content():
    otro = {**BOTONES, "body": "Otro texto", "buttons": [{"id": "x", "title": "Sí"}, {"id": "y", "title": "No"}]}
    assert it.forma_y_variables(otro)[:2] == it.forma_y_variables(BOTONES)[:2]


@pytest.mark.parametrize(
    "malo",
    [
        {"type": "button", "body": "", "buttons": [{"id": "a", "title": "A"}]},
        {"type": "button", "body": "x", "buttons": []},
        {"type": "list", "body": "x", "button": "", "rows": [{"id": "a", "title": "A"}]},
        {"type": "list", "body": "x", "button": "B", "rows": []},
        {"type": "carousel", "body": "x"},
    ],
)
def test_interactivo_que_no_sirve(malo):
    with pytest.raises(ChannelError):
        it.forma_y_variables(malo)


def test_los_topes_de_twilio_recortan():
    largo = {
        "type": "list",
        "body": "x" * 2000,
        "button": "b" * 40,
        "rows": [{"id": "i" * 300, "title": "t" * 40, "description": "d" * 100}],
    }
    _, _, v = it.forma_y_variables(largo)
    assert len(v["1"]) == it.MAX_CUERPO and len(v["2"]) == it.MAX_TITULO_BOTON
    assert len(v["3"]) == it.MAX_TITULO_FILA and len(v["4"]) == it.MAX_ID and len(v["5"]) == it.MAX_DESCRIPCION


# ---------------------------------------------------------------- envío (caché simulada)
class _Cache:
    def __init__(self, sids):
        self.sids = list(sids)
        self.olvidadas = 0

    async def content_de(self, client, tenant_id, connection_id, forma, tipos, variables):
        return self.sids[0]

    async def olvidar(self, tenant_id, connection_id, forma):
        self.olvidadas += 1
        self.sids.pop(0)


def _twilio(respuestas):
    enviados = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        enviados.append(form)
        status, body = respuestas.pop(0)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler), enviados


async def test_enviar_manda_el_content_con_sus_variables(monkeypatch):
    cache = _Cache([HX1])
    monkeypatch.setattr(it, "_content_de", cache.content_de)
    transport, enviados = _twilio([(201, {"sid": "SM" + "9" * 32})])
    sid = await it.enviar(uuid.uuid4(), uuid.uuid4(), CREDS, "whatsapp:+573001234567", BOTONES, transport=transport)
    assert sid == "SM" + "9" * 32
    [form] = enviados
    assert form["ContentSid"] == HX1 and form["To"] == "whatsapp:+573001234567"
    assert json.loads(form["ContentVariables"])["3"] == "opt:a"


async def test_content_borrado_se_recrea_una_vez(monkeypatch):
    cache = _Cache([HX1, HX2])
    monkeypatch.setattr(it, "_content_de", cache.content_de)
    monkeypatch.setattr(it, "_olvidar", cache.olvidar)
    transport, enviados = _twilio([(404, {"code": 20404, "message": "not found"}), (201, {"sid": "SM" + "8" * 32})])
    sid = await it.enviar(uuid.uuid4(), uuid.uuid4(), CREDS, "whatsapp:+573001234567", LISTA, transport=transport)
    assert sid == "SM" + "8" * 32 and cache.olvidadas == 1
    assert [f["ContentSid"] for f in enviados] == [HX1, HX2]


async def test_otro_error_no_recrea(monkeypatch):
    cache = _Cache([HX1, HX2])
    monkeypatch.setattr(it, "_content_de", cache.content_de)
    monkeypatch.setattr(it, "_olvidar", cache.olvidar)
    transport, _ = _twilio([(400, {"code": 63016, "message": "fuera de la ventana"})])
    with pytest.raises(ProviderError) as exc:
        await it.enviar(uuid.uuid4(), uuid.uuid4(), CREDS, "whatsapp:+573001234567", BOTONES, transport=transport)
    assert exc.value.category == "WINDOW" and cache.olvidadas == 0


async def test_dos_404_seguidos_no_recrean_sin_fin(monkeypatch):
    cache = _Cache([HX1, HX2])
    monkeypatch.setattr(it, "_content_de", cache.content_de)
    monkeypatch.setattr(it, "_olvidar", cache.olvidar)
    transport, enviados = _twilio([(404, {"code": 20404}), (404, {"code": 20404})])
    with pytest.raises(ProviderError):
        await it.enviar(uuid.uuid4(), uuid.uuid4(), CREDS, "whatsapp:+573001234567", BOTONES, transport=transport)
    assert len(enviados) == 2 and cache.olvidadas == 1


# ---------------------------------------------------------------- outbox
def _fixture_twilio(monkeypatch, adapter):
    import core.tasks.outbound_send as mod

    msg, tenant_id, calls = _send_one_fixture(monkeypatch, adapter=adapter)

    async def _creds(*a, **k):
        return ("twilio", dict(CREDS))

    monkeypatch.setattr(mod, "resolve_credentials", _creds)
    return msg, tenant_id, calls


async def test_outbox_manda_el_interactivo_de_twilio_por_su_content(monkeypatch):
    from core.tasks.outbound_send import _send_one

    llamadas = []

    async def _enviar(tenant_id, connection_id, credentials, to, interactive, *, outbound=None):
        llamadas.append((to, interactive["type"], outbound))
        return "SM" + "7" * 32

    async def _nunca(*a, **k):
        raise AssertionError("no debe salir como texto ni por send_interactive de Meta")

    adapter = SimpleNamespace(send_text=_nunca, send_image=_nunca, send_interactive=_nunca)
    monkeypatch.setattr(it, "enviar", _enviar)
    msg, tenant_id, _ = _fixture_twilio(monkeypatch, adapter)
    msg.interactive = dict(BOTONES)
    assert await _send_one(msg.id, tenant_id) is None
    assert msg.status == "sent" and msg.external_message_id == "SM" + "7" * 32
    assert llamadas == [("whatsapp:+573001234567", "button", adapter)]


async def test_outbox_interactivo_de_twilio_que_no_sirve_es_permanente_sin_texto(monkeypatch):
    from core.tasks.outbound_send import _send_one

    async def _nunca(*a, **k):
        raise AssertionError("no debe caer a texto")

    adapter = SimpleNamespace(send_text=_nunca, send_image=_nunca, send_content_template=_nunca)
    msg, tenant_id, calls = _fixture_twilio(monkeypatch, adapter)
    msg.interactive = {"type": "carousel", "body": "x"}
    error = await _send_one(msg.id, tenant_id)
    assert isinstance(error, ProviderError) and error.provider == "twilio" and error.category == "PERMANENT"
    assert calls["release"] == 1 and msg.status == "queued"
