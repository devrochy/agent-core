"""Salida de Instagram con Facebook Login (ADR-0152, HU-CHAN-04): límites propios, adaptador de la Send API
de la Página vinculada y registro por canal. Unitarios sin BD ni red."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx
import pytest

import core.gateway.channels.messenger.outbound as outbound_mod
from core.gateway.channels.instagram.outbound import InstagramMetaOutbound
from core.gateway.channels.messenger.outbound import MessengerMetaOutbound
from core.gateway.channels.registry import outbound_adapter_for
from core.gateway.meta_errors import GraphError
from core.gateway.meta_instagram import MAX_TEXT_BYTES, InstagramSendError, igsid_de, validar_texto
from core.gateway.meta_messenger import MessengerSendError, plan_de_envio
from core.shared.exceptions import ChannelError

AHORA = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ límites propios
def test_el_limite_del_texto_es_en_bytes_no_en_caracteres():
    validar_texto("a" * MAX_TEXT_BYTES)  # justo en el límite
    with pytest.raises(InstagramSendError, match="1000 bytes"):
        validar_texto("a" * (MAX_TEXT_BYTES + 1))
    # 600 vocales con tilde son 600 caracteres pero 1200 bytes.
    with pytest.raises(InstagramSendError):
        validar_texto("á" * 600)


def test_un_mensaje_vacio_no_se_envia():
    with pytest.raises(InstagramSendError, match="vacío"):
        validar_texto("   ")


def test_el_igsid_sale_de_la_clave_del_cliente():
    assert igsid_de("instagram:17841400000000000:IGSID9") == "IGSID9"
    assert igsid_de("instagram:IGSID9") == "IGSID9"  # clave sin cuenta (anterior)


def test_el_error_de_ventana_nombra_instagram():
    vencida = AHORA - timedelta(minutes=1)
    with pytest.raises(MessengerSendError, match="Instagram"):
        plan_de_envio(service_window_expires_at=vencida, humano=False, ahora=AHORA, canal="instagram")
    with pytest.raises(MessengerSendError, match="Instagram ya no permite"):
        plan_de_envio(service_window_expires_at=AHORA - timedelta(days=9), humano=True, ahora=AHORA, canal="instagram")


# ------------------------------------------------------------------ el adaptador
class _Respuesta:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status_code = status
        self._body = body if body is not None else {"recipient_id": "IGSID9", "message_id": "m_ig"}
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
    return {"page_id": "page-vinculada", "access_token": "tok-pagina", "graph_api_version": "v21.0", **extra}


@pytest.mark.asyncio
async def test_responde_por_la_pagina_vinculada_con_el_igsid(cliente):
    mid = await InstagramMetaOutbound().send_text(_creds(), "instagram:IG1:IGSID9", "Hola")

    assert mid == "m_ig"
    (llamada,) = cliente.llamadas
    assert llamada["url"].endswith("/v21.0/page-vinculada/messages")
    assert llamada["headers"]["Authorization"] == "Bearer tok-pagina"
    assert llamada["json"] == {
        "recipient": {"id": "IGSID9"}, "messaging_type": "RESPONSE", "message": {"text": "Hola"},
    }


@pytest.mark.asyncio
async def test_fuera_de_ventana_una_persona_manda_con_human_agent(cliente):
    vencida = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await InstagramMetaOutbound().send_text(
        _creds(service_window_expires_at=vencida, human="true"), "instagram:IG1:IGSID9", "Te escribe una persona"
    )
    body = cliente.llamadas[0]["json"]
    assert (body["messaging_type"], body["tag"]) == ("MESSAGE_TAG", "HUMAN_AGENT")


@pytest.mark.asyncio
async def test_fuera_de_ventana_el_asistente_falla_permanente_sin_llamar_a_meta(cliente):
    vencida = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    with pytest.raises(GraphError) as exc:
        await InstagramMetaOutbound().send_text(
            _creds(service_window_expires_at=vencida, human="false"), "instagram:IG1:IGSID9", "Hola"
        )
    assert exc.value.category == "PERMANENT" and "Instagram" in str(exc.value)
    assert cliente.llamadas == []


@pytest.mark.asyncio
async def test_un_texto_largo_falla_permanente_sin_llamar_a_meta(cliente):
    with pytest.raises(GraphError) as exc:
        await InstagramMetaOutbound().send_text(_creds(), "instagram:IG1:IGSID9", "x" * (MAX_TEXT_BYTES + 1))
    assert exc.value.category == "PERMANENT"
    assert cliente.llamadas == []


@pytest.mark.asyncio
async def test_sin_credenciales_falla_sin_llamar_a_meta(cliente):
    with pytest.raises(ChannelError, match="Instagram"):
        await InstagramMetaOutbound().send_text({"page_id": "p"}, "instagram:IG1:IGSID9", "Hola")
    assert cliente.llamadas == []


@pytest.mark.asyncio
async def test_las_imagenes_fallan_explicito_y_permanente():
    with pytest.raises(GraphError) as exc:
        await InstagramMetaOutbound().send_image(_creds(), "instagram:IG1:IGSID9", "https://x/y.png")
    assert exc.value.category == "PERMANENT"


@pytest.mark.asyncio
async def test_un_error_de_meta_sale_clasificado(cliente):
    cliente.respuesta = _Respuesta(400, {"error": {"message": "(#100) No matching user found", "code": 100}})
    with pytest.raises(GraphError) as exc:
        await InstagramMetaOutbound().send_text(_creds(), "instagram:IG1:IGSID9", "Hola")
    assert exc.value.code == 100


# ------------------------------------------------------------------ el registro por canal
def test_meta_instagram_usa_su_adaptador_y_no_el_de_messenger_ni_el_de_whatsapp():
    adaptador = outbound_adapter_for("meta", "instagram")
    assert isinstance(adaptador, InstagramMetaOutbound)
    assert adaptador.channel == "instagram"
    assert type(outbound_adapter_for("meta", "messenger")) is MessengerMetaOutbound


# ------------------------------------------------------------------ alta: lo puro
def test_dice_exactamente_que_variables_faltan_para_instagram():
    from types import SimpleNamespace

    from core.gateway.meta_instagram_alta import config_id_de_instagram, faltan_para_instagram

    base = {"meta_app_id": "app", "meta_app_secret": "s", "meta_instagram_config_id": "",
            "meta_messenger_config_id": "msgr", "meta_config_id": "wa"}
    # La de Messenger y la de WhatsApp NO sustituyen a la de Instagram: son otros permisos.
    assert config_id_de_instagram(SimpleNamespace(**base)) == ""
    assert faltan_para_instagram(SimpleNamespace(**base)) == ["META_INSTAGRAM_CONFIG_ID"]
    todo = SimpleNamespace(**{**base, "meta_instagram_config_id": "ig"})
    assert faltan_para_instagram(todo) == []
    vacia = SimpleNamespace(**{**base, "meta_app_id": "", "meta_app_secret": ""})
    assert faltan_para_instagram(vacia) == ["META_APP_ID", "META_APP_SECRET", "META_INSTAGRAM_CONFIG_ID"]


def test_los_campos_suscritos_son_los_de_nuestra_app_no_los_de_otras_de_la_pagina():
    from core.gateway.meta_instagram_alta import _campos_de_la_app

    entradas = [
        {"id": "otra-app", "subscribed_fields": ["feed", "messages"]},
        {"id": "nuestra", "subscribed_fields": ["messages", "messaging_seen"]},
    ]
    assert _campos_de_la_app(entradas, "nuestra") == {"messages", "messaging_seen"}
    assert _campos_de_la_app([], "nuestra") == set()
