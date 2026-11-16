"""Paquete `channel-telegram` (ADR-0199, PR core 1): cliente de la Bot API, mapeo de errores y límites.

Tests puros, sin red: el cliente HTTP usa `httpx.MockTransport`. El webhook, el alta y los adaptadores llegan en los PR
siguientes.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from agentcore_channel_telegram import PROVIDER
from agentcore_channel_telegram.client import TelegramClient, mascara, redactar, token_valido
from agentcore_channel_telegram.errors import categoria, error_from_response
from agentcore_channel_telegram.limites import (
    ALLOWED_UPDATES,
    CALLBACK_DATA_MAX_BYTES,
    cabe_callback_data,
    nuevo_secreto_webhook,
    secreto_webhook_valido,
)

from core.gateway.channels.base import ProviderError

TOKEN = "123456789:AAH" + "x" * 32
SECRETO = "s" * 43


def _cliente(handler) -> TelegramClient:
    return TelegramClient(TOKEN, transport=httpx.MockTransport(handler))


def _ok(result) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _error(status: int, description: str, **extra) -> httpx.Response:
    return httpx.Response(status, json={"ok": False, "error_code": status, "description": description, **extra})


def test_provider():
    assert PROVIDER == "telegram"


# ------------------------------------------------------------------ token
@pytest.mark.parametrize(
    ("token", "valido"),
    [
        (TOKEN, True),
        ("123:corto", False),
        ("abc:" + "x" * 35, False),
        ("123456789:" + "x" * 35 + "/../getMe", False),
        ("", False),
        (None, False),
    ],
)
def test_token_valido(token, valido):
    assert token_valido(token) is valido


def test_un_token_mal_formado_no_arma_una_url():
    with pytest.raises(ProviderError) as exc:
        TelegramClient("123/../x:" + "y" * 35)
    assert exc.value.category == "CONFIG"


def test_mascara_y_redactar_nunca_muestran_el_token():
    assert mascara(TOKEN) == "…:" + TOKEN[-4:]
    assert TOKEN.split(":")[1] not in mascara(TOKEN)
    assert mascara(None) == "…"
    texto = f"GET https://api.telegram.org/bot{TOKEN}/getMe y /file/bot{TOKEN}/voice/a.oga"
    assert TOKEN not in redactar(texto)
    assert "/bot[redactado]/getMe" in redactar(texto)


def test_el_repr_del_cliente_no_lleva_el_token():
    assert TOKEN not in repr(_cliente(lambda r: _ok({})))


# ------------------------------------------------------------------ llamadas
async def test_get_me_va_a_la_ruta_del_bot_por_post_json():
    vistos: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistos.append(request)
        return _ok({"id": 123456789, "is_bot": True, "username": "mi_bot", "first_name": "Mi bot"})

    me = await _cliente(handler).get_me()
    assert me["id"] == 123456789
    (req,) = vistos
    assert req.method == "POST"
    assert req.url.host == "api.telegram.org"
    assert req.url.path == f"/bot{TOKEN}/getMe"


async def test_set_webhook_manda_secreto_y_solo_mensajes_y_callbacks():
    cuerpos: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cuerpos.append(json.loads(request.content))
        return _ok(True)

    assert await _cliente(handler).set_webhook("https://api.ejemplo.com/webhooks/telegram/abc", SECRETO) is True
    (cuerpo,) = cuerpos
    assert cuerpo == {
        "url": "https://api.ejemplo.com/webhooks/telegram/abc",
        "secret_token": SECRETO,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }
    assert list(ALLOWED_UPDATES) == ["message", "callback_query"]


@pytest.mark.parametrize(
    ("url", "secreto"),
    [("http://inseguro/webhook", SECRETO), ("https://ok/webhook", "con espacios"), ("https://ok/webhook", "")],
)
async def test_set_webhook_rechaza_url_sin_https_o_secreto_invalido(url, secreto):
    llamadas = []
    with pytest.raises(ProviderError) as exc:
        await _cliente(lambda r: llamadas.append(r) or _ok(True)).set_webhook(url, secreto)
    assert exc.value.category == "CONFIG" and llamadas == []


async def test_send_message_con_teclado_inline():
    cuerpos: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cuerpos.append(json.loads(request.content))
        return _ok({"message_id": 7, "chat": {"id": 42}})

    teclado = {"inline_keyboard": [[{"text": "Sí", "callback_data": "si"}]]}
    msg = await _cliente(handler).send_message(42, "Hola", reply_markup=teclado)
    assert msg["message_id"] == 7
    assert cuerpos == [{"chat_id": 42, "text": "Hola", "reply_markup": teclado}]


async def test_send_voice_con_bytes_va_como_multipart():
    vistos: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistos.append(request)
        return _ok({"message_id": 8})

    await _cliente(handler).send_voice(42, b"OggS-audio")
    (req,) = vistos
    assert req.url.path.endswith("/sendVoice")
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b"OggS-audio" in req.content and b'name="chat_id"' in req.content


async def test_download_file_corta_en_el_maximo():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/file/bot{TOKEN}/voice/file_1.oga"
        return httpx.Response(200, content=b"a" * 100)

    cliente = _cliente(handler)
    assert await cliente.download_file("voice/file_1.oga", max_bytes=100) == b"a" * 100
    with pytest.raises(ProviderError) as exc:
        await cliente.download_file("voice/file_1.oga", max_bytes=99)
    assert exc.value.category == "CONTENT"


@pytest.mark.parametrize("file_path", ["../bot/x", "/etc/passwd", "voice/a.oga?x=1", ""])
async def test_download_file_rechaza_rutas_raras(file_path):
    with pytest.raises(ProviderError) as exc:
        await _cliente(lambda r: _ok({})).download_file(file_path)
    assert exc.value.category == "CONFIG"


# ------------------------------------------------------------------ errores de red
async def test_sin_conexion_es_transitorio_y_no_arrastra_el_token(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"fallo hacia {request.url}", request=request)

    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderError) as exc:
        await _cliente(handler).send_message(42, "Hola")
    assert exc.value.category == "TRANSIENT"
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert TOKEN not in str(exc.value) and TOKEN not in caplog.text


@pytest.mark.parametrize(("metodo", "categoria_esperada"), [("send", "AMBIGUOUS"), ("info", "TRANSIENT")])
async def test_timeout_de_lectura_ambiguo_solo_en_envios(metodo, categoria_esperada):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=request)

    cliente = _cliente(handler)
    with pytest.raises(ProviderError) as exc:
        if metodo == "send":
            await cliente.send_message(42, "Hola")
        else:
            await cliente.get_webhook_info()
    assert exc.value.category == categoria_esperada


@pytest.mark.parametrize(("metodo", "categoria_esperada"), [("send", "AMBIGUOUS"), ("info", "TRANSIENT")])
async def test_2xx_sin_ok_queda_clasificado(metodo, categoria_esperada):
    cliente = _cliente(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError) as exc:
        if metodo == "send":
            await cliente.send_message(42, "Hola")
        else:
            await cliente.get_webhook_info()
    assert exc.value.category == categoria_esperada


# ------------------------------------------------------------------ mapeo de errores
@pytest.mark.parametrize(
    ("status", "descripcion", "esperada"),
    [
        (401, "Unauthorized", "AUTH"),
        (403, "Forbidden: bot was blocked by the user", "RECIPIENT"),
        (403, "Forbidden: user is deactivated", "RECIPIENT"),
        (403, "Forbidden: bot can't initiate conversation with a user", "RECIPIENT"),
        (400, "Bad Request: chat not found", "RECIPIENT"),
        (400, "Bad Request: PEER_ID_INVALID", "RECIPIENT"),
        (400, "Bad Request: message text is empty", "CONTENT"),
        (404, "Not Found", "CONFIG"),
        (409, "Conflict: can't use getUpdates method while webhook is active", "CONFIG"),
        (429, "Too Many Requests: retry after 5", "RATE_LIMIT"),
        (500, "Internal Server Error", "TRANSIENT"),
        (502, "Bad Gateway", "TRANSIENT"),
    ],
)
def test_categoria(status, descripcion, esperada):
    assert categoria(status, descripcion) == esperada


def test_bloqueado_nunca_es_baja_ni_404_revoca():
    assert categoria(403, "Forbidden: bot was blocked by the user") != "OPT_OUT"
    assert categoria(404, "Not Found") != "AUTH"


def test_429_trae_retry_after():
    error = error_from_response(
        429, {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 7}}
    )
    assert error.category == "RATE_LIMIT" and error.retry_after == 7.0 and error.code == 429
    assert error.provider == "telegram"


def test_error_sin_cuerpo_json_cae_por_estado():
    error = error_from_response(503, None)
    assert error.category == "TRANSIENT" and error.code is None


def test_la_descripcion_se_recorta_y_no_lleva_tokens():
    error = error_from_response(400, {"description": f"x {TOKEN} " + "y" * 1000})
    assert TOKEN not in str(error) and len(str(error)) < 400


async def test_el_cliente_lanza_el_error_clasificado():
    cliente = _cliente(lambda r: _error(403, "Forbidden: bot was blocked by the user"))
    with pytest.raises(ProviderError) as exc:
        await cliente.send_message(42, "Hola")
    assert exc.value.category == "RECIPIENT" and exc.value.http_status == 403


# ------------------------------------------------------------------ límites
def test_callback_data_cuenta_bytes():
    assert cabe_callback_data("a" * CALLBACK_DATA_MAX_BYTES)
    assert not cabe_callback_data("a" * (CALLBACK_DATA_MAX_BYTES + 1))
    assert not cabe_callback_data("ñ" * 33)  # 66 bytes
    assert not cabe_callback_data("")


def test_secreto_del_webhook():
    secreto = nuevo_secreto_webhook()
    assert secreto_webhook_valido(secreto) and secreto != nuevo_secreto_webhook()
    assert not secreto_webhook_valido("a" * 257)
    assert not secreto_webhook_valido("a" * 31) and secreto_webhook_valido("a" * 32)
    assert not secreto_webhook_valido("con.punto")
    assert not secreto_webhook_valido(None)
