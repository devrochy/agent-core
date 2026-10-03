"""Canal de chat web embebible (HU-CHAN-02).

Tests puros: registry, adaptador de salida y validación de Origin. El
comportamiento de los endpoints (Turnstile, rate limit, tenant por clave) vive
en `core/api/routers/web_chat.py` y se prueba ahí sin tocar red/BD.
"""

from __future__ import annotations

import asyncio

from core.api.routers.web_chat import _rate_limited, origin_allowed
from core.gateway.channels.registry import outbound_adapter_for, providers


def test_registry_incluye_el_canal_web():
    assert "web" in providers()


def test_registry_resuelve_el_adaptador_web():
    adapter = outbound_adapter_for("web")
    assert adapter.provider == "web"


def test_web_outbound_devuelve_id_sintetico():
    from agentcore_channel_web.adapters import WebOutbound

    wamid = asyncio.run(WebOutbound().send_text({}, "web:visitor-1", "hola"))
    assert wamid == "web:web:visitor-1"


def test_origin_exacto_permitido():
    allowed = ["https://negocio.com", "https://www.negocio.com"]
    assert origin_allowed("https://negocio.com", allowed)
    assert origin_allowed("https://www.negocio.com", allowed)
    # Barra final y mayúsculas no cambian la comparación.
    assert origin_allowed("https://NEGOCIO.com/", allowed)


def test_origin_no_permitido():
    allowed = ["https://negocio.com"]
    assert not origin_allowed("", allowed)
    assert not origin_allowed("https://otro.com", allowed)
    assert not origin_allowed("http://negocio.com", allowed)  # esquema distinto
    assert not origin_allowed("https://evil.com.negocio.com", allowed)


def test_rate_limit_corta_tras_el_tope(monkeypatch):
    from core.api.routers import web_chat

    monkeypatch.setattr(web_chat, "_hits", {})
    monkeypatch.setattr(web_chat, "_RATE_LIMIT", 3)
    ip = "10.0.0.1"
    assert not _rate_limited(ip)
    assert not _rate_limited(ip)
    assert not _rate_limited(ip)
    assert _rate_limited(ip)  # cuarta supera el tope de 3
