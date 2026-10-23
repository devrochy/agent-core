"""Salida de Messenger (ADR-0152, HU-CHAN-01): la decisión de ventana y etiqueta, el adaptador de
la Send API y el registro por canal. Unitarios sin BD ni red."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx
import pytest

import core.gateway.channels.messenger.outbound as outbound_mod
from core.gateway.channels.messenger.outbound import MessengerMetaOutbound
from core.gateway.channels.registry import outbound_adapter_for
from core.gateway.channels.whatsapp.outbound import WhatsAppMetaOutbound
from core.gateway.meta_errors import GraphError
from core.gateway.meta_messenger import (
    ALLOWED_MESSAGE_TAGS,
    MessengerSendError,
    PlanDeEnvio,
    plan_de_envio,
)

AHORA = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ la decisión
def test_dentro_de_la_ventana_se_responde_con_response():
    plan = plan_de_envio(service_window_expires_at=AHORA + timedelta(hours=3), humano=False, ahora=AHORA)
    assert plan == PlanDeEnvio("RESPONSE", None)


def test_sin_ventana_registrada_se_responde_como_dentro_de_la_ventana():
    assert plan_de_envio(service_window_expires_at=None, humano=False, ahora=AHORA).messaging_type == "RESPONSE"


def test_fuera_de_la_ventana_el_asistente_no_puede_escribir():
    with pytest.raises(MessengerSendError, match="asistente no puede escribir"):
        plan_de_envio(service_window_expires_at=AHORA - timedelta(minutes=1), humano=False, ahora=AHORA)


def test_fuera_de_la_ventana_una_persona_usa_human_agent():
    plan = plan_de_envio(service_window_expires_at=AHORA - timedelta(days=2), humano=True, ahora=AHORA)
    assert plan == PlanDeEnvio("MESSAGE_TAG", "HUMAN_AGENT")


def test_human_agent_llega_hasta_7_dias_desde_el_ultimo_mensaje_del_cliente():
    # Último mensaje del cliente = fin de la ventana - 24 h. El día 7 todavía sirve; pasado, no.
    dentro = AHORA - timedelta(days=6) + timedelta(seconds=1)  # último mensaje hace 7 d - 1 s
    fuera = AHORA - timedelta(days=6) - timedelta(seconds=1)  # hace 7 d + 1 s
    assert plan_de_envio(service_window_expires_at=dentro, humano=True, ahora=AHORA).tag == "HUMAN_AGENT"
    with pytest.raises(MessengerSendError, match="7 días"):
        plan_de_envio(service_window_expires_at=fuera, humano=True, ahora=AHORA)


def test_solo_human_agent_esta_permitida():
    # CUSTOMER_FEEDBACK exige su plantilla; las tres retiradas en 2026 no deben volver nunca.
    assert ALLOWED_MESSAGE_TAGS == frozenset({"HUMAN_AGENT"})
    for retirada in ("CONFIRMED_EVENT_UPDATE", "ACCOUNT_UPDATE", "POST_PURCHASE_UPDATE", "CUSTOMER_FEEDBACK"):
        assert retirada not in ALLOWED_MESSAGE_TAGS


# ------------------------------------------------------------------ el adaptador
class _Respuesta:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status_code = status
        self._body = body if body is not None else {"recipient_id": "psid-1", "message_id": "m_abc"}
        self.headers = httpx.Headers()
        self.text = str(self._body)

    def json(self):
        return self._body


class _Cliente:
    llamadas: ClassVar[list[dict]] = []
    respuesta: ClassVar[_Respuesta] = _Respuesta()

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, *, json, params, headers):
        type(self).llamadas.append({"url": url, "json": json, "params": params, "headers": headers})
        return type(self).respuesta


@pytest.fixture
def cliente(monkeypatch):
    _Cliente.llamadas = []
    _Cliente.respuesta = _Respuesta()
    monkeypatch.setattr(outbound_mod.httpx, "AsyncClient", _Cliente)
    return _Cliente


def _creds(**extra):
    return {"page_id": "page-1", "access_token": "tok-pagina", "graph_api_version": "v21.0", **extra}


@pytest.mark.asyncio
async def test_envia_a_la_pagina_con_su_token_y_devuelve_el_id_del_mensaje(cliente):
    futuro = (datetime.now(UTC) + timedelta(hours=2)).isoformat()

    message_id = await MessengerMetaOutbound().send_text(
        _creds(service_window_expires_at=futuro), "messenger:psid-1", "Hola"
    )

    assert message_id == "m_abc"
    (llamada,) = cliente.llamadas
    assert llamada["url"] == "https://graph.facebook.com/v21.0/page-1/messages"
    assert llamada["headers"] == {"Authorization": "Bearer tok-pagina"}
    assert llamada["json"] == {
        "recipient": {"id": "psid-1"}, "messaging_type": "RESPONSE", "message": {"text": "Hola"},
    }
    assert "appsecret_proof" not in llamada["params"]


@pytest.mark.asyncio
async def test_con_app_secret_envia_el_appsecret_proof(cliente):
    await MessengerMetaOutbound().send_text(_creds(app_secret="secreto"), "messenger:psid-1", "Hola")

    assert len(cliente.llamadas[0]["params"]["appsecret_proof"]) == 64


@pytest.mark.asyncio
async def test_fuera_de_ventana_una_persona_manda_con_human_agent(cliente):
    vencida = (datetime.now(UTC) - timedelta(days=2)).isoformat()

    await MessengerMetaOutbound().send_text(
        _creds(service_window_expires_at=vencida, human="true"), "messenger:psid-1", "Te escribe una persona"
    )

    body = cliente.llamadas[0]["json"]
    assert (body["messaging_type"], body["tag"]) == ("MESSAGE_TAG", "HUMAN_AGENT")


@pytest.mark.asyncio
async def test_fuera_de_ventana_el_asistente_falla_permanente_y_no_llama_a_meta(cliente):
    vencida = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    with pytest.raises(GraphError) as exc:
        await MessengerMetaOutbound().send_text(
            _creds(service_window_expires_at=vencida, human="false"), "messenger:psid-1", "Hola"
        )

    assert exc.value.category == "PERMANENT"
    assert cliente.llamadas == []


@pytest.mark.asyncio
async def test_una_fecha_sin_zona_horaria_se_toma_como_utc(cliente):
    sin_zona = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()

    await MessengerMetaOutbound().send_text(_creds(service_window_expires_at=sin_zona), "messenger:psid-1", "Hola")

    assert cliente.llamadas[0]["json"]["messaging_type"] == "RESPONSE"


@pytest.mark.asyncio
async def test_un_error_de_meta_sale_clasificado(cliente):
    cliente.respuesta = _Respuesta(400, {"error": {"message": "(#100) No matching user found", "code": 100}})

    with pytest.raises(GraphError) as exc:
        await MessengerMetaOutbound().send_text(_creds(), "messenger:psid-1", "Hola")

    assert exc.value.code == 100


@pytest.mark.asyncio
async def test_sin_credenciales_falla_sin_llamar_a_meta(cliente):
    from core.shared.exceptions import ChannelError

    with pytest.raises(ChannelError):
        await MessengerMetaOutbound().send_text({"page_id": "p"}, "messenger:psid-1", "Hola")
    assert cliente.llamadas == []


@pytest.mark.asyncio
async def test_las_imagenes_fallan_explicito_y_permanente():
    with pytest.raises(GraphError) as exc:
        await MessengerMetaOutbound().send_image(_creds(), "messenger:psid-1", "https://x/y.png")
    assert exc.value.category == "PERMANENT"


# ------------------------------------------------------------------ el registro por canal
def test_meta_messenger_usa_su_adaptador_y_meta_whatsapp_el_de_siempre():
    assert isinstance(outbound_adapter_for("meta", "messenger"), MessengerMetaOutbound)
    assert isinstance(outbound_adapter_for("meta", "whatsapp"), WhatsAppMetaOutbound)
    assert isinstance(outbound_adapter_for("meta"), WhatsAppMetaOutbound)


def test_instagram_sin_adaptador_no_cae_al_de_whatsapp():
    # Mandaría el identificador del cliente de Instagram como si fuera un teléfono.
    with pytest.raises(KeyError, match="meta/instagram"):
        outbound_adapter_for("meta", "instagram")
