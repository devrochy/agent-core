"""Descarga de media entrante de Twilio (ADR-0186, PR-4): auth básica solo contra api.twilio.com, un redirect a un
host permitido sin credenciales, tope de bytes y errores clasificados. Sin red: `httpx.MockTransport`."""

from __future__ import annotations

import httpx
import pytest

from core.media.errors import MediaProviderError
from core.media.twilio_media import descargar_media_twilio

AC = "AC" + "a" * 32
URL = f"https://api.twilio.com/2010-04-01/Accounts/{AC}/Messages/MM{'1' * 32}/Media/ME{'2' * 32}"
CDN = "https://media.twiliocdn.com/abc/archivo"
S3 = "https://s3-external-1.amazonaws.com/media.twiliocdn.com/abc"


def _transport(handler):
    vistos: list[httpx.Request] = []

    def _wrap(request: httpx.Request) -> httpx.Response:
        vistos.append(request)
        return handler(request)

    return httpx.MockTransport(_wrap), vistos


async def test_sigue_un_redirect_sin_llevar_las_credenciales():
    def handler(request):
        if request.url.host == "api.twilio.com":
            return httpx.Response(307, headers={"location": CDN})
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    transport, vistos = _transport(handler)
    media = await descargar_media_twilio(URL, AC, "tok", transport=transport)
    assert (media.contenido, media.mime_type, media.bytes) == (b"\x89PNG", "image/png", 4)
    assert vistos[0].headers["authorization"].startswith("Basic ")
    assert "authorization" not in vistos[1].headers


async def test_sin_redirect_y_con_el_tipo_del_webhook():
    transport, _ = _transport(lambda r: httpx.Response(200, content=b"audio"))
    media = await descargar_media_twilio(
        URL, AC, "tok", mime_esperado="audio/ogg", transport=transport
    )
    assert media.mime_type == "audio/ogg"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.twilio.com/x",
        "https://evil.example.com/x",
        "https://api.twilio.com.evil.com/x",
        "",
    ],
)
async def test_url_inicial_que_no_es_de_twilio(url):
    transport, vistos = _transport(lambda r: httpx.Response(200, content=b"x"))
    with pytest.raises(MediaProviderError) as exc:
        await descargar_media_twilio(url, AC, "tok", transport=transport)
    assert exc.value.code == "media_url_invalida"
    assert vistos == []


@pytest.mark.parametrize(
    "destino", ["https://evil.example.com/x", "http://media.twiliocdn.com/x", "https://10.0.0.1/x"]
)
async def test_redirect_a_un_host_no_permitido(destino):
    transport, vistos = _transport(lambda r: httpx.Response(302, headers={"location": destino}))
    with pytest.raises(MediaProviderError) as exc:
        await descargar_media_twilio(URL, AC, "tok", transport=transport)
    assert exc.value.code == "media_url_invalida"
    assert len(vistos) == 1


async def test_tope_de_bytes(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "media_max_bytes", 10)

    def handler(request):
        if request.url.host == "api.twilio.com":
            return httpx.Response(307, headers={"location": CDN})
        return httpx.Response(200, content=b"x" * 50, headers={"content-type": "image/jpeg"})

    transport, _ = _transport(handler)
    with pytest.raises(MediaProviderError) as exc:
        await descargar_media_twilio(URL, AC, "tok", transport=transport)
    assert exc.value.code == "media_demasiado_grande"


@pytest.mark.parametrize(
    ("status", "transitorio"), [(404, False), (401, False), (429, True), (503, True)]
)
async def test_errores_http_clasificados(status, transitorio):
    transport, _ = _transport(lambda r: httpx.Response(status))
    with pytest.raises(MediaProviderError) as exc:
        await descargar_media_twilio(URL, AC, "tok", transport=transport)
    assert exc.value.transient is transitorio


async def test_sin_credenciales_no_sale_a_red():
    transport, vistos = _transport(lambda r: httpx.Response(200, content=b"x"))
    with pytest.raises(MediaProviderError) as exc:
        await descargar_media_twilio(URL, AC, "", transport=transport)
    assert exc.value.code == "media_sin_token"
    assert vistos == []


@pytest.mark.parametrize(
    "url",
    [
        f"https://api.twilio.com/2010-04-01/Accounts/{'AC' + 'b' * 32}/Messages/MM{'1' * 32}/Media/ME{'2' * 32}",
        f"https://api.twilio.com/2010-04-01/Accounts/{AC}.json",
        f"https://api.twilio.com/2010-04-01/Accounts/{AC}/Messages/MM{'1' * 32}/Media/ME{'2' * 32}?x=1",
    ],
)
async def test_solo_media_de_un_mensaje_de_la_cuenta(url):
    transport, vistos = _transport(lambda r: httpx.Response(200, content=b"x"))
    with pytest.raises(MediaProviderError) as exc:
        await descargar_media_twilio(url, AC, "tok", transport=transport)
    assert exc.value.code == "media_url_invalida"
    assert vistos == []


@pytest.mark.parametrize(
    ("destino", "ok"),
    [
        (S3, True),
        ("https://evil.s3bucket.amazonaws.com.evil.io/x", False),
        ("https://ec2-1-2-3-4.compute.amazonaws.com/x", False),
    ],
)
async def test_s3_acotado(destino, ok):
    def handler(request):
        if request.url.host == "api.twilio.com":
            return httpx.Response(307, headers={"location": destino})
        return httpx.Response(200, content=b"x", headers={"content-type": "image/png"})

    transport, _ = _transport(handler)
    if ok:
        assert (await descargar_media_twilio(URL, AC, "tok", transport=transport)).bytes == 1
    else:
        with pytest.raises(MediaProviderError):
            await descargar_media_twilio(URL, AC, "tok", transport=transport)


async def test_tope_sin_redirect(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "media_max_bytes", 10)
    transport, _ = _transport(lambda r: httpx.Response(200, content=b"x" * 50))
    with pytest.raises(MediaProviderError) as exc:
        await descargar_media_twilio(URL, AC, "tok", mime_esperado="image/png", transport=transport)
    assert exc.value.code == "media_demasiado_grande"
