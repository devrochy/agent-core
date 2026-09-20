"""Resumable Upload API: MIME no permitido rechazado local, el paso 1 usa
el APP access token, el handle se usa tal cual (sin transformarlo)."""

from __future__ import annotations

import uuid

import pytest

import core.gateway.meta_template_upload as upload_mod
from core.gateway.meta_template_upload import (
    TemplateUploadError,
    start_upload_session,
    upload_file_chunk,
    validate_mime_type,
)


def test_mime_no_permitido_falla_local():
    with pytest.raises(TemplateUploadError):
        validate_mime_type("application/zip")


@pytest.mark.parametrize("mime", ["application/pdf", "image/jpeg", "image/jpg", "image/png", "video/mp4"])
def test_mime_permitidos_pasan(mime):
    validate_mime_type(mime)


@pytest.mark.asyncio
async def test_start_upload_session_usa_app_access_token(monkeypatch):
    captured = {}

    async def _fake_graph_fetch(path, **kwargs):
        captured["path"] = path
        captured["token"] = kwargs["token"]
        captured["query"] = kwargs["query"]
        return {"id": "upload:SESSION123"}

    monkeypatch.setattr(upload_mod, "graph_fetch", _fake_graph_fetch)

    upload_id = await start_upload_session(
        "app-id-1", "app-access-token-xyz", "app-secret", "v21.0", uuid.uuid4(),
        file_name="cabecera.png", file_length=100, file_type="image/png",
    )
    assert upload_id == "upload:SESSION123"
    assert captured["path"] == "/app-id-1/uploads"
    assert captured["token"] == "app-access-token-xyz"
    assert captured["query"]["file_type"] == "image/png"


@pytest.mark.asyncio
async def test_upload_file_chunk_usa_oauth_no_bearer_y_devuelve_handle_tal_cual(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"h": "HANDLE-ABC-NO-TRANSFORMADO"}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, content=None):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    handle = await upload_file_chunk("upload:SESSION123", "app-access-token-xyz", b"bytes")
    assert handle == "HANDLE-ABC-NO-TRANSFORMADO"
    assert captured["headers"]["Authorization"] == "OAuth app-access-token-xyz"
    assert "upload:SESSION123" in captured["url"]
