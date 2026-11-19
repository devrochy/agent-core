"""Canal de chat web embebible (HU-CHAN-02).

Tests puros: registry, adaptador de salida, validación de Origin y rate limit.
El comportamiento de los endpoints (Turnstile, rate limit, tenant por clave)
vive en `core/api/routers/web_chat.py` y se prueba ahí sin tocar red/BD.

El rate limit es por (widget, IP) en Redis: una IP que escribe a un widget no
consume el cupo de otro tenant. Además, las keys inexistentes cuentan por IP y,
con ese cupo lleno, la IP recibe 429 antes de tocar la base. La mecánica de la
ventana (purga, TTL, tope del set, fail-open) se prueba en `test_rate_limit.py`;
aquí, cómo arma el widget sus cupos. Redis es el doble de `conftest.py`.
"""

from __future__ import annotations

import asyncio

import pytest
from starlette.requests import Request

from core.api.routers import web_chat
from core.api.routers.web_chat import origin_allowed
from core.gateway.channels.registry import outbound_adapter_for, providers

KEY_A = "widgetKeyTenantA_0123456789abcdef"
KEY_B = "widgetKeyTenantB_0123456789abcdef"


def test_registry_incluye_el_canal_web():
    assert "web" in providers()


def test_registry_resuelve_el_adaptador_web():
    adapter = outbound_adapter_for("web")
    assert adapter.provider == "web"


def test_web_outbound_devuelve_id_sintetico():
    from agentcore_channel_web.adapters import WebOutbound

    wamid = asyncio.run(WebOutbound().send_text({}, "web:visitor-1", "hola"))
    otro = asyncio.run(WebOutbound().send_text({}, "web:visitor-1", "chao"))
    # Único por mensaje (índice único por tenant y `external_message_id`), sin el visitante.
    assert wamid.startswith("web:") and otro.startswith("web:") and wamid != otro
    assert "visitor" not in wamid


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


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch, rl_redis):
    monkeypatch.setattr(web_chat, "_RATE_LIMIT", 3)
    monkeypatch.setattr(web_chat, "_MISS_LIMIT", 2)
    return rl_redis


async def test_rate_limit_corta_tras_el_tope(redis) -> None:
    ip = "203.0.113.1"
    assert not await web_chat._rate_limited(KEY_A, ip)
    assert not await web_chat._rate_limited(KEY_A, ip)
    assert not await web_chat._rate_limited(KEY_A, ip)
    assert await web_chat._rate_limited(KEY_A, ip)  # cuarta supera el tope de 3


async def test_rate_limit_de_un_tenant_no_afecta_a_otro(redis) -> None:
    ip = "203.0.113.1"
    for _ in range(5):
        await web_chat._rate_limited(KEY_A, ip)
    assert await web_chat._rate_limited(KEY_A, ip)

    # Misma IP, widget de otro tenant: cupo intacto.
    assert not await web_chat._rate_limited(KEY_B, ip)
    assert not await web_chat._rate_limited(KEY_B, ip)
    assert not await web_chat._rate_limited(KEY_B, ip)
    assert await web_chat._rate_limited(KEY_B, ip)


async def test_rate_limit_de_una_ip_no_afecta_a_otra(redis) -> None:
    for _ in range(5):
        await web_chat._rate_limited(KEY_A, "203.0.113.1")
    assert not await web_chat._rate_limited(KEY_A, "203.0.113.2")


async def test_rate_limit_key_invalida_no_crea_claves_arbitrarias(redis) -> None:
    await web_chat._rate_limited("../../" + "x" * 500, "203.0.113.1")
    await web_chat._rate_limited("a" * 129, "203.0.113.1")
    assert redis.keys() == {f"{web_chat._RATE_KEY_PREFIX}:!invalid:203.0.113.1"}


async def test_rate_limit_trafico_valido_no_gasta_el_cupo_de_fallos(redis) -> None:
    ip = "203.0.113.1"
    for key in (KEY_A, KEY_B):
        for _ in range(3):
            assert not await web_chat._rate_limited(key, ip)
    assert f"{web_chat._MISS_KEY_PREFIX}:{ip}" not in redis.keys()


def _request(peer: str, xff: str = "") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/v1/public/web-chat/{KEY_A}/messages",
            "headers": headers,
            "client": (peer, 40000),
        }
    )


async def test_post_message_limita_por_ip_real_detras_del_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detrás de un proxy de confianza el cupo es de la IP del visitante, no la
    del proxy; y al superarlo responde 429 `rate_limited` sin resolver el tenant."""
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "trusted_proxy_cidrs", "10.0.0.9/32")
    seen: list[tuple[str, str]] = []

    async def _limited(key: str, ip: str) -> bool:
        seen.append((key, ip))
        return True

    async def _no_resolve(key: str):
        raise AssertionError("no debe tocar la base si ya está limitado")

    monkeypatch.setattr(web_chat, "_rate_limited", _limited)
    monkeypatch.setattr(web_chat, "_resolve_by_key", _no_resolve)

    res = await web_chat.post_message(KEY_A, _request("10.0.0.9", "198.51.100.7"))
    assert res.status_code == 429
    assert res.body == b'{"error":"rate_limited"}'
    assert seen == [(KEY_A, "198.51.100.7")]


async def test_post_message_keys_inexistentes_cortan_por_ip(
    monkeypatch: pytest.MonkeyPatch, redis
) -> None:
    """Una key al azar por request abre un cupo nuevo cada vez; los 404 suman
    por IP y, con ese cupo lleno, la IP recibe 429 sin resolver nada. Otra IP
    sigue pasando."""
    resolved: list[str] = []

    async def _unknown(key: str):
        resolved.append(key)
        return None

    monkeypatch.setattr(web_chat, "_resolve_by_key", _unknown)

    for i in range(web_chat._MISS_LIMIT):
        res = await web_chat.post_message(f"azar{i:028d}", _request("198.51.100.7"))
        assert res.status_code == 404
    res = await web_chat.post_message("azar-otra-mas-0000000000000000", _request("198.51.100.7"))
    assert res.status_code == 429
    assert res.body == b'{"error":"rate_limited"}'
    assert len(resolved) == web_chat._MISS_LIMIT  # el 429 no llegó a la base

    res = await web_chat.post_message("azar-otra-mas-0000000000000000", _request("198.51.100.8"))
    assert res.status_code == 404


async def test_post_message_keys_conocidas_no_las_corta_el_cupo_de_fallos(
    monkeypatch: pytest.MonkeyPatch, redis
) -> None:
    """Visitantes de distintos tenants detrás de la misma IP (CGNAT): si uno
    llena el cupo de fallos con keys al azar, el widget que ya se resolvió
    (conocido) sigue pasando; una key nunca resuelta, no."""

    async def _resolve(key: str):
        if key == KEY_A:
            return "tenant-a", {"enabled": False}  # 403 tras pasar el limitador
        return None

    monkeypatch.setattr(web_chat, "_resolve_by_key", _resolve)
    ip = "198.51.100.7"

    res = await web_chat.post_message(KEY_A, _request(ip))
    assert res.status_code == 403  # resuelta: queda anotada como conocida
    for i in range(web_chat._MISS_LIMIT):
        await web_chat.post_message(f"azar{i:028d}", _request(ip))

    res = await web_chat.post_message(KEY_A, _request(ip))
    assert res.status_code == 403  # pasó el limitador
    res = await web_chat.post_message(KEY_B, _request(ip))
    assert res.status_code == 429  # no conocida: el cupo de fallos la corta
