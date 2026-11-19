import hashlib

import pytest

from core.config import get_settings
from core.media import meta_media
from core.media.errors import MediaProviderError


@pytest.fixture(autouse=True)
def _settings_frescas():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Respuesta:
    def __init__(self, status_code: int, payload=None, *, body: bytes = b"") -> None:
        self.status_code = status_code
        self._payload = payload
        self._body = body
        self.text = str(payload)

    def json(self):  # type: ignore[no-untyped-def]
        if self._payload is None:
            raise ValueError("sin json")
        return self._payload

    async def aiter_bytes(self):  # type: ignore[no-untyped-def]
        for i in range(0, len(self._body), 3):
            yield self._body[i : i + 3]


class _Stream:
    def __init__(self, response: _Respuesta) -> None:
        self.response = response

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self.response

    async def __aexit__(self, *_exc):  # type: ignore[no-untyped-def]
        return None


class _Cliente:
    def __init__(self, metadata: _Respuesta, descarga: _Respuesta) -> None:
        self.metadata = metadata
        self.descarga = descarga

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *_exc):  # type: ignore[no-untyped-def]
        return None

    async def get(self, *_a, **_k):  # type: ignore[no-untyped-def]
        return self.metadata

    def stream(self, *_a, **_k):  # type: ignore[no-untyped-def]
        return _Stream(self.descarga)


def _cliente(monkeypatch: pytest.MonkeyPatch, metadata: _Respuesta, descarga: _Respuesta) -> None:
    monkeypatch.setattr(meta_media.httpx, "AsyncClient", lambda **_k: _Cliente(metadata, descarga))


async def test_descarga_ok_y_verifica_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    contenido = b"audio-bytes"
    sha = hashlib.sha256(contenido).hexdigest()
    _cliente(
        monkeypatch,
        _Respuesta(200, {"url": "https://lookaside.fbsbx.com/whatsapp/x", "mime_type": "audio/ogg", "sha256": sha}),
        _Respuesta(200, body=contenido),
    )
    media = await meta_media.descargar_media("123", "token")
    assert media.contenido == contenido
    assert media.mime_type == "audio/ogg"
    assert media.sha256 == sha


async def test_rechaza_host_que_no_es_de_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    _cliente(
        monkeypatch,
        _Respuesta(200, {"url": "https://evil.example/x", "mime_type": "audio/ogg"}),
        _Respuesta(200, body=b"x"),
    )
    with pytest.raises(MediaProviderError) as exc:
        await meta_media.descargar_media("123", "token")
    assert exc.value.code == "media_url_invalida"


async def test_rechaza_archivo_demasiado_grande(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIA_MAX_BYTES", "10")
    _cliente(
        monkeypatch,
        _Respuesta(
            200,
            {
                "url": "https://lookaside.fbsbx.com/whatsapp/x",
                "mime_type": "audio/ogg",
                "file_size": 50,
            },
        ),
        _Respuesta(200, body=b"x"),
    )
    with pytest.raises(MediaProviderError) as exc:
        await meta_media.descargar_media("123", "token")
    assert exc.value.code == "media_demasiado_grande"


async def test_metadata_429_es_transitoria(monkeypatch: pytest.MonkeyPatch) -> None:
    _cliente(monkeypatch, _Respuesta(429, {"error": {"message": "rate"}}), _Respuesta(200, body=b"x"))
    with pytest.raises(MediaProviderError) as exc:
        await meta_media.descargar_media("123", "token")
    assert exc.value.transient is True
