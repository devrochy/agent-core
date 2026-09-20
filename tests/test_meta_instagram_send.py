"""Validaciones locales de envío de Instagram: texto/medios exceden límites
fallan ANTES de llamar a Meta; sin conversación previa da el mensaje de
"no cold outreach" (distinto del de ventana vencida); la ruta (b) con el
feature flag apagado nunca llama a `graph.instagram.com`; los tags fuera de
`IG_ALLOWED_TAGS` no existen en el código."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import core.gateway.meta_instagram as ig_mod
from core.gateway.meta_instagram import (
    IG_ALLOWED_TAGS,
    InstagramSendError,
    send_instagram_message_route_b,
    validate_instagram_message,
)

_RETIRED_MESSENGER_TAGS = {"CONFIRMED_EVENT_UPDATE", "ACCOUNT_UPDATE", "POST_PURCHASE_UPDATE", "CUSTOMER_FEEDBACK"}


def test_ig_allowed_tags_es_solo_human_agent():
    """Instagram es más restrictivo que Messenger a propósito -- CUSTOMER_
    FEEDBACK (permitido en Messenger) NO aplica acá según el ticket."""
    assert IG_ALLOWED_TAGS == {"HUMAN_AGENT"}


def test_tags_no_permitidos_no_existen_como_literal():
    source = inspect.getsource(ig_mod)
    for tag in _RETIRED_MESSENGER_TAGS - {"HUMAN_AGENT"}:
        assert f'"{tag}"' not in source and f"'{tag}'" not in source


def test_texto_muy_largo_falla_local():
    with pytest.raises(InstagramSendError, match="1000"):
        validate_instagram_message("x" * 1000)


def test_mas_de_10_imagenes_falla_local():
    media = [{"type": "image/png", "size_bytes": 100} for _ in range(11)]
    with pytest.raises(InstagramSendError, match="10 imágenes"):
        validate_instagram_message("hola", media)


def test_imagen_no_png_ni_jpeg_falla_local():
    with pytest.raises(InstagramSendError, match="PNG o JPEG"):
        validate_instagram_message("hola", [{"type": "image/gif", "size_bytes": 100}])


def test_imagen_mayor_a_8mb_falla_local():
    with pytest.raises(InstagramSendError, match="8MB"):
        validate_instagram_message("hola", [{"type": "image/png", "size_bytes": 9 * 1024 * 1024}])


def test_video_mayor_a_25mb_falla_local():
    with pytest.raises(InstagramSendError, match="25MB"):
        validate_instagram_message("hola", [{"type": "video/mp4", "size_bytes": 26 * 1024 * 1024}])


@pytest.mark.asyncio
async def test_sin_conversacion_previa_da_mensaje_de_cold_outreach(monkeypatch):
    monkeypatch.setattr(ig_mod, "get_settings", lambda: SimpleNamespace(instagram_direct_send_enabled=True, meta_app_secret="s"))

    async def _debe_no_llamarse(*a, **k):
        raise AssertionError("no debe llamar a Meta sin conversación previa")

    monkeypatch.setattr(ig_mod, "graph_fetch", _debe_no_llamarse)

    with pytest.raises(InstagramSendError, match="cold outreach"):
        await send_instagram_message_route_b(
            "ig-user-1", "igsid-1", "hola", "tok", service_window_expires_at=None
        )


@pytest.mark.asyncio
async def test_ruta_b_con_flag_apagado_nunca_llama_a_instagram(monkeypatch):
    monkeypatch.setattr(ig_mod, "get_settings", lambda: SimpleNamespace(instagram_direct_send_enabled=False))

    async def _debe_no_llamarse(*a, **k):
        raise AssertionError("no debe llamar a graph.instagram.com con el flag apagado")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _debe_no_llamarse)

    expires = datetime.now(UTC) + timedelta(hours=1)
    with pytest.raises(InstagramSendError, match="no está habilitado"):
        await send_instagram_message_route_b(
            "ig-user-1", "igsid-1", "hola", "tok", service_window_expires_at=expires
        )


@pytest.mark.asyncio
async def test_ventana_vencida_sin_confirmacion_pide_agente_humano(monkeypatch):
    monkeypatch.setattr(ig_mod, "get_settings", lambda: SimpleNamespace(instagram_direct_send_enabled=True, meta_app_secret="s"))

    async def _debe_no_llamarse(*a, **k):
        raise AssertionError("no debe llamar a Meta sin confirmación humana")

    monkeypatch.setattr(ig_mod, "graph_fetch", _debe_no_llamarse)

    expired = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(InstagramSendError, match="agente humano"):
        await send_instagram_message_route_b(
            "ig-user-1", "igsid-1", "hola", "tok", service_window_expires_at=expired, human_confirmed=False
        )
