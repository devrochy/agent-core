"""Canal de chat web embebible (HU-CHAN-02).

Tests puros: registry, adaptador de salida, validación de Origin y rate limit.
El comportamiento de los endpoints (Turnstile, rate limit, tenant por clave)
vive en `core/api/routers/web_chat.py` y se prueba ahí sin tocar red/BD.

El rate limit es por (widget, IP) en Redis: una IP que escribe a un widget no
consume el cupo de otro tenant, y las entradas vencen solas (ventana deslizante
+ TTL de la clave). Redis se reemplaza por un doble en memoria con reloj
controlado que implementa solo lo que usa `_rate_limited` (sorted set +
`EXPIRE` en un pipeline).
"""

from __future__ import annotations

import asyncio

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
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


class _Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakeRedis:
    """Sorted sets con TTL, leyendo el tiempo de `clock`."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.zsets: dict[str, dict[str, float]] = {}
        self.expires_at: dict[str, float] = {}

    def _expire_due(self) -> None:
        for key, at in list(self.expires_at.items()):
            if at <= self.clock():
                self.zsets.pop(key, None)
                self.expires_at.pop(key, None)

    def keys(self) -> set[str]:
        self._expire_due()
        return set(self.zsets)

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def zremrangebyscore(self, key: str, lo: str, hi: float) -> _FakePipeline:
        self.ops.append(("zremrangebyscore", key, hi))
        return self

    def zadd(self, key: str, mapping: dict[str, float]) -> _FakePipeline:
        self.ops.append(("zadd", key, mapping))
        return self

    def zremrangebyrank(self, key: str, start: int, stop: int) -> _FakePipeline:
        self.ops.append(("zremrangebyrank", key, start, stop))
        return self

    def zcard(self, key: str) -> _FakePipeline:
        self.ops.append(("zcard", key))
        return self

    def expire(self, key: str, seconds: int) -> _FakePipeline:
        self.ops.append(("expire", key, seconds))
        return self

    async def execute(self) -> list:
        r = self.redis
        r._expire_due()
        results: list = []
        for op, key, *args in self.ops:
            zset = r.zsets.setdefault(key, {})
            if op == "zremrangebyscore":
                before = len(zset)
                for member, score in list(zset.items()):
                    if score <= args[0]:
                        del zset[member]
                results.append(before - len(zset))
            elif op == "zadd":
                zset.update(args[0])
                results.append(len(args[0]))
            elif op == "zremrangebyrank":
                ordered = sorted(zset, key=zset.__getitem__)
                start, stop = args
                stop = len(ordered) + stop if stop < 0 else stop
                doomed = ordered[start : stop + 1] if stop >= 0 else []  # como Redis
                for member in doomed:
                    del zset[member]
                results.append(len(doomed))
            elif op == "zcard":
                results.append(len(zset))
            elif op == "expire":
                r.expires_at[key] = r.clock() + args[0]
                results.append(True)
            if not zset:
                r.zsets.pop(key, None)
        return results


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()
    monkeypatch.setattr(web_chat, "_clock", c)
    return c


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> _FakeRedis:
    fake = _FakeRedis(clock)
    monkeypatch.setattr(web_chat, "get_redis", lambda: fake)
    monkeypatch.setattr(web_chat, "_RATE_LIMIT", 3)
    return fake


async def test_rate_limit_corta_tras_el_tope(redis: _FakeRedis) -> None:
    ip = "203.0.113.1"
    assert not await web_chat._rate_limited(KEY_A, ip)
    assert not await web_chat._rate_limited(KEY_A, ip)
    assert not await web_chat._rate_limited(KEY_A, ip)
    assert await web_chat._rate_limited(KEY_A, ip)  # cuarta supera el tope de 3


async def test_rate_limit_de_un_tenant_no_afecta_a_otro(redis: _FakeRedis) -> None:
    ip = "203.0.113.1"
    for _ in range(5):
        await web_chat._rate_limited(KEY_A, ip)
    assert await web_chat._rate_limited(KEY_A, ip)

    # Misma IP, widget de otro tenant: cupo intacto.
    assert not await web_chat._rate_limited(KEY_B, ip)
    assert not await web_chat._rate_limited(KEY_B, ip)
    assert not await web_chat._rate_limited(KEY_B, ip)
    assert await web_chat._rate_limited(KEY_B, ip)


async def test_rate_limit_de_una_ip_no_afecta_a_otra(redis: _FakeRedis) -> None:
    for _ in range(5):
        await web_chat._rate_limited(KEY_A, "203.0.113.1")
    assert not await web_chat._rate_limited(KEY_A, "203.0.113.2")


async def test_rate_limit_la_ventana_desliza(redis: _FakeRedis, clock: _Clock) -> None:
    ip = "203.0.113.1"
    for _ in range(3):
        await web_chat._rate_limited(KEY_A, ip)
    clock.now += web_chat._RATE_WINDOW_S - 1
    assert await web_chat._rate_limited(KEY_A, ip)  # todavía en la ventana

    clock.now += web_chat._RATE_WINDOW_S
    assert not await web_chat._rate_limited(KEY_A, ip)  # los hits viejos se purgaron
    assert len(redis.zsets[web_chat._rate_key(KEY_A, ip)]) == 1


async def test_rate_limit_purga_ips_inactivas(redis: _FakeRedis, clock: _Clock) -> None:
    for i in range(50):
        await web_chat._rate_limited(KEY_A, f"198.51.100.{i}")
    assert len(redis.keys()) == 50

    clock.now += web_chat._RATE_WINDOW_S
    assert redis.keys() == set()


async def test_rate_limit_una_inundacion_no_infla_el_set(redis: _FakeRedis) -> None:
    ip = "203.0.113.1"
    for _ in range(500):
        await web_chat._rate_limited(KEY_A, ip)
    assert await web_chat._rate_limited(KEY_A, ip)
    assert len(redis.zsets[web_chat._rate_key(KEY_A, ip)]) == web_chat._RATE_LIMIT + 1


async def test_rate_limit_key_invalida_no_crea_claves_arbitrarias(redis: _FakeRedis) -> None:
    await web_chat._rate_limited("../../" + "x" * 500, "203.0.113.1")
    await web_chat._rate_limited("a" * 129, "203.0.113.1")
    assert redis.keys() == {f"{web_chat._RATE_KEY_PREFIX}:_invalid:203.0.113.1"}


async def test_rate_limit_redis_caido_deja_pasar(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Down:
        def pipeline(self, transaction: bool = True):
            raise RedisConnectionError("down")

    monkeypatch.setattr(web_chat, "get_redis", lambda: _Down())
    assert not await web_chat._rate_limited(KEY_A, "203.0.113.1")


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
