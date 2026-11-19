"""Instagram Login (ruta b de HU-CHAN-04, ADR-0152): dirección de autorización, canje, refresco, suscripción y envío
contra un HTTP simulado, el adaptador de salida por la cuenta y la firma del webhook con el secreto de la app de
Instagram. Unitarios sin BD ni red."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import core.gateway.meta_instagram_login as login
from core.gateway.channels.instagram.outbound import InstagramMetaOutbound
from core.gateway.meta_errors import GraphError
from core.gateway.meta_instagram import (
    ESTADO_CONECTADA,
    ESTADO_DEGRADADA,
    con_estado,
    estado_de,
)


def _settings(**kw):
    base = {
        "instagram_app_id": "ig-app", "instagram_app_secret": "ig-secreto",
        "instagram_login_redirect_uri": "https://panel.test/conexiones/instagram/callback",
        "instagram_login_enabled": True, "graph_api_version": "v21.0",
    }
    return SimpleNamespace(**{**base, **kw})


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(login, "get_settings", lambda: _settings())


# ------------------------------------------------------------------ HTTP simulado
class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.headers, self.text = status, body if body is not None else {}, httpx.Headers(), "x"

    def json(self):
        return self._body


class _Http:
    """Un `httpx.AsyncClient` falso: guarda cada llamada y responde por (método, fragmento de la URL)."""

    llamadas: ClassVar[list[dict]] = []
    respuestas: ClassVar[dict[tuple[str, str], _Resp]] = {}

    def __init__(self, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def _responder(self, metodo, url, **kw):
        type(self).llamadas.append({"metodo": metodo, "url": url, **kw})
        for (m, fragmento), resp in type(self).respuestas.items():
            if m == metodo and fragmento in url:
                return resp
        raise AssertionError(f"llamada inesperada: {metodo} {url}")

    async def get(self, url, **kw):
        return self._responder("GET", url, **kw)

    async def post(self, url, **kw):
        return self._responder("POST", url, **kw)


@pytest.fixture
def http(monkeypatch):
    _Http.llamadas, _Http.respuestas = [], {}
    monkeypatch.setattr(login.httpx, "AsyncClient", _Http)
    return _Http


# ------------------------------------------------------------------ configuración y dirección
def test_la_direccion_de_autorizacion_usa_la_app_de_instagram_y_sus_permisos():
    url = login.url_de_autorizacion("el-state")
    partes = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(partes.query).items()}
    assert f"{partes.scheme}://{partes.netloc}{partes.path}" == login.IG_AUTHORIZE_URL
    assert q["client_id"] == "ig-app" and q["state"] == "el-state" and q["response_type"] == "code"
    assert q["redirect_uri"] == "https://panel.test/conexiones/instagram/callback"
    # Solo los permisos de Instagram Login; nada de los de Facebook Login en el mismo diálogo.
    assert q["scope"] == "instagram_business_basic,instagram_business_manage_messages"
    assert "pages_" not in q["scope"] and q["enable_fb_login"] == "0"
    assert "ig-secreto" not in url  # el secreto nunca viaja en el navegador


def test_dice_exactamente_que_variables_faltan_para_instagram_login():
    assert login.faltan_para_instagram_login(_settings()) == []
    vacia = _settings(instagram_app_id="", instagram_app_secret="", instagram_login_redirect_uri="")
    assert login.faltan_para_instagram_login(vacia) == [
        "INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET", "INSTAGRAM_LOGIN_REDIRECT_URI",
    ]


def test_la_ruta_esta_apagada_hasta_prenderla_y_luego_pide_su_configuracion():
    from core.gateway.meta_instagram_login_alta import login_disponible

    assert login_disponible(_settings(instagram_login_enabled=False)) == ["INSTAGRAM_LOGIN_ENABLED"]
    assert login_disponible(_settings()) == []
    assert login_disponible(_settings(instagram_app_id="")) == ["INSTAGRAM_APP_ID"]


# ------------------------------------------------------------------ canje, refresco y suscripción
@pytest.mark.asyncio
async def test_canjea_el_codigo_por_formulario_y_devuelve_token_y_cuenta(http):
    http.respuestas[("POST", "api.instagram.com/oauth/access_token")] = _Resp(
        200, {"access_token": "corto", "user_id": 17841400000000001}
    )

    token, user_id = await login.canjear_codigo("codigo")

    assert (token, user_id) == ("corto", "17841400000000001")
    (llamada,) = http.llamadas
    assert llamada["data"]["grant_type"] == "authorization_code" and llamada["data"]["code"] == "codigo"
    assert llamada["data"]["client_secret"] == "ig-secreto" and "json" not in llamada  # formulario, no JSON


@pytest.mark.asyncio
async def test_acepta_la_respuesta_envuelta_en_data_de_versiones_viejas(http):
    http.respuestas[("POST", "oauth/access_token")] = _Resp(200, {"data": [{"access_token": "t", "user_id": "9"}]})
    assert await login.canjear_codigo("c") == ("t", "9")


@pytest.mark.asyncio
async def test_un_canje_rechazado_sale_clasificado_y_sin_el_secreto(http):
    http.respuestas[("POST", "oauth/access_token")] = _Resp(400, {"error_message": "code expired", "code": 400})
    with pytest.raises(GraphError) as exc:
        await login.canjear_codigo("c")
    assert "ig-secreto" not in str(exc.value)


@pytest.mark.asyncio
async def test_el_token_largo_dura_lo_que_dice_meta_o_60_dias(http):
    http.respuestas[("GET", "graph.instagram.com/access_token")] = _Resp(200, {"access_token": "largo", "expires_in": 5184000})
    token, vence = await login.canjear_por_token_largo("corto")
    assert token == "largo"
    assert timedelta(days=59) < vence - datetime.now(UTC) <= timedelta(days=60)
    assert http.llamadas[0]["params"]["grant_type"] == "ig_exchange_token"

    http.respuestas[("GET", "graph.instagram.com/access_token")] = _Resp(200, {"access_token": "largo"})
    _, vence = await login.canjear_por_token_largo("corto")
    assert timedelta(days=59) < vence - datetime.now(UTC) <= timedelta(days=60)


@pytest.mark.asyncio
async def test_el_refresco_pide_otros_60_dias_con_ig_refresh_token(http):
    http.respuestas[("GET", "refresh_access_token")] = _Resp(200, {"access_token": "nuevo", "expires_in": 5184000})
    nuevo, vence = await login.refrescar_token("viejo")
    assert nuevo == "nuevo" and vence > datetime.now(UTC) + timedelta(days=59)
    assert http.llamadas[0]["params"] == {"grant_type": "ig_refresh_token", "access_token": "viejo"}


@pytest.mark.asyncio
async def test_un_refresco_rechazado_levanta_y_uno_sin_token_tambien(http):
    http.respuestas[("GET", "refresh_access_token")] = _Resp(400, {"error": {"message": "Session has expired", "code": 190}})
    with pytest.raises(GraphError):
        await login.refrescar_token("viejo")
    http.respuestas[("GET", "refresh_access_token")] = _Resp(200, {})
    with pytest.raises(GraphError):
        await login.refrescar_token("viejo")


@pytest.mark.asyncio
async def test_la_cuenta_es_el_user_id_y_el_token_va_en_la_cabecera(http):
    http.respuestas[("GET", "graph.instagram.com/me")] = _Resp(200, {"user_id": "1784", "id": "app-scoped", "username": "tienda"})
    assert await login.datos_de_la_cuenta("tok") == ("1784", "tienda")
    assert http.llamadas[0]["headers"] == {"Authorization": "Bearer tok"} and "access_token" not in http.llamadas[0].get("params", {})

    http.respuestas[("GET", "graph.instagram.com/me")] = _Resp(200, {"id": "solo-id"})
    assert (await login.datos_de_la_cuenta("tok"))[0] == "solo-id"


@pytest.mark.asyncio
async def test_suscribe_con_subscribed_fields_y_solo_cuenta_si_confirma_messages(http):
    base = "graph.instagram.com/v21.0/1784/subscribed_apps"
    http.respuestas[("POST", base)] = _Resp(200, {"success": True})
    http.respuestas[("GET", base)] = _Resp(200, {"data": [{"subscribed_fields": ["messages", "messaging_seen"]}]})

    suscrita, campos = await login.suscribir_y_verificar("1784", "tok", "v21.0")

    assert suscrita is True and campos == ("messages", "messaging_seen")
    post = next(c for c in http.llamadas if c["metodo"] == "POST")
    assert "subscribed_fields" in post["params"] and "fields" not in post["params"]  # el nombre correcto

    http.respuestas[("GET", base)] = _Resp(200, {"data": [{"subscribed_fields": ["messaging_seen"]}]})
    assert (await login.suscribir_y_verificar("1784", "tok", "v21.0"))[0] is False
    http.respuestas[("GET", base)] = _Resp(200, {"data": []})
    assert await login.suscribir_y_verificar("1784", "tok", "v21.0") == (False, ())


# ------------------------------------------------------------------ envío por la cuenta
def _creds(**extra):
    return {"login_route": "ig_instagram_login", "ig_user_id": "1784", "access_token": "tok-cuenta",
            "graph_api_version": "v21.0", **extra}


@pytest.mark.asyncio
async def test_por_instagram_login_se_escribe_desde_la_cuenta_no_desde_una_pagina(http):
    http.respuestas[("POST", "graph.instagram.com/v21.0/1784/messages")] = _Resp(200, {"message_id": "m_1"})

    mid = await InstagramMetaOutbound().send_text(_creds(), "instagram:1784:IGSID9", "Hola")

    assert mid == "m_1"
    (llamada,) = http.llamadas
    assert llamada["headers"] == {"Authorization": "Bearer tok-cuenta"}
    assert llamada["json"] == {"recipient": {"id": "IGSID9"}, "messaging_type": "RESPONSE", "message": {"text": "Hola"}}
    assert "graph.facebook.com" not in llamada["url"]


@pytest.mark.asyncio
async def test_por_instagram_login_las_mismas_reglas_de_ventana_y_de_texto(http):
    vencida = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with pytest.raises(GraphError) as sin_derecho:
        await InstagramMetaOutbound().send_text(_creds(service_window_expires_at=vencida, human="false"), "instagram:1784:I", "Hola")
    with pytest.raises(GraphError) as largo:
        await InstagramMetaOutbound().send_text(_creds(), "instagram:1784:I", "x" * 1001)
    assert sin_derecho.value.category == largo.value.category == "PERMANENT"
    assert http.llamadas == []  # ni una llamada a Meta

    http.respuestas[("POST", "/messages")] = _Resp(200, {"message_id": "m_2"})
    await InstagramMetaOutbound().send_text(_creds(service_window_expires_at=vencida, human="true"), "instagram:1784:I", "Persona")
    cuerpo = http.llamadas[0]["json"]
    assert (cuerpo["messaging_type"], cuerpo["tag"]) == ("MESSAGE_TAG", "HUMAN_AGENT")


@pytest.mark.asyncio
async def test_por_instagram_login_sin_cuenta_o_sin_token_falla_sin_llamar(http):
    from core.shared.exceptions import ChannelError

    for creds in ({"login_route": "ig_instagram_login", "access_token": "t"}, _creds(access_token="")):
        with pytest.raises(ChannelError):
            await InstagramMetaOutbound().send_text(creds, "instagram:1784:I", "Hola")
    assert http.llamadas == []


# ------------------------------------------------------------------ estado de la cuenta
def test_el_estado_se_escribe_sin_pisar_el_resto_y_una_conectada_no_lleva_motivo():
    raw = {"subscribed_fields": ["messages"]}
    degradada = con_estado(raw, ESTADO_DEGRADADA, "reconecta")
    assert degradada == {"subscribed_fields": ["messages"], "status": "degraded", "status_reason": "reconecta"}
    assert estado_de(degradada) == ("degraded", "reconecta")
    sana = con_estado(degradada, ESTADO_CONECTADA, "ya no aplica")
    assert sana == {"subscribed_fields": ["messages"], "status": "connected"}
    assert estado_de(None) == ("connected", None) and estado_de({}) == ("connected", None)


# ------------------------------------------------------------------ firma del webhook con el secreto de Instagram
def _firma(cuerpo: bytes, secreto: str) -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


def _config_de_secretos(monkeypatch, *, meta="secreto-meta", instagram="secreto-ig"):
    import core.gateway.connections as connections

    monkeypatch.setattr(
        connections, "get_settings",
        lambda: SimpleNamespace(whatsapp_app_secret="", meta_app_secret=meta, instagram_app_secret=instagram),
    )


def _grupo(objeto: str, entry_id: str):
    return SimpleNamespace(
        clave=(entry_id, None), entry_id=entry_id, sub_id=None, events=[SimpleNamespace(object_type=objeto)],
        route=SimpleNamespace(tenant_id=None, conflict=False),
    )


@pytest.mark.asyncio
async def test_la_firma_del_secreto_de_instagram_solo_autoriza_grupos_de_instagram(monkeypatch):
    """Si el secreto de la app de Instagram se filtrara, no serviría para inyectar mensajes de WhatsApp ni de Messenger."""
    from core.gateway.webhooks import _firmantes_del_post

    _config_de_secretos(monkeypatch)
    cuerpo = json.dumps({"object": "instagram"}).encode()

    firmantes = await _firmantes_del_post(cuerpo, _firma(cuerpo, "secreto-ig"), [])

    assert firmantes is not None and firmantes.plataforma is False
    assert firmantes.autoriza(_grupo("instagram", "1784")) is True
    assert firmantes.autoriza(_grupo("whatsapp_business_account", "99")) is False
    assert firmantes.autoriza(_grupo("page", "55")) is False


@pytest.mark.asyncio
async def test_la_firma_de_la_app_de_meta_sigue_autorizando_todo_y_una_ajena_nada(monkeypatch):
    from core.gateway.webhooks import _firmantes_del_post

    _config_de_secretos(monkeypatch)
    cuerpo = b"{}"

    de_meta = await _firmantes_del_post(cuerpo, _firma(cuerpo, "secreto-meta"), [])
    assert de_meta is not None and de_meta.plataforma is True
    for objeto in ("instagram", "page", "whatsapp_business_account"):
        assert de_meta.autoriza(_grupo(objeto, "1")) is True

    ajena = await _firmantes_del_post(cuerpo, _firma(cuerpo, "otro-secreto"), [])
    assert ajena is not None and not ajena.plataforma and not ajena.objetos and not ajena.grupos


def test_el_tope_de_grupos_cuenta_la_firma_de_la_app_de_instagram_como_plataforma(monkeypatch):
    from core.gateway.webhooks import _firmo_la_plataforma

    _config_de_secretos(monkeypatch)
    cuerpo = b"{}"
    assert _firmo_la_plataforma(cuerpo, _firma(cuerpo, "secreto-meta")) is True
    assert _firmo_la_plataforma(cuerpo, _firma(cuerpo, "secreto-ig")) is True
    assert _firmo_la_plataforma(cuerpo, _firma(cuerpo, "otro-secreto")) is False


def test_sin_secreto_de_instagram_no_hay_firma_de_instagram(monkeypatch):
    import core.gateway.connections as connections

    _config_de_secretos(monkeypatch, instagram="")
    assert connections.instagram_platform_secret() == ""
