"""Rate limit del intake público de leads (`POST /api/pack/pack_commerce/leads/{slug}`).

El cupo es por (tenant, IP): una IP que manda leads a un tenant no consume el
cupo de otro. Además, los slugs inexistentes cuentan por IP y, con ese cupo
lleno, la IP recibe 429 antes de tocar la base. La mecánica de la ventana
(purga, TTL, tope del set, fail-open) se prueba en `test_rate_limit.py`; aquí,
cómo arma el intake sus cupos. Redis es el doble de `conftest.py`.
"""

from __future__ import annotations

import json

import pytest
from pack_commerce import api
from starlette.requests import Request


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch, rl_redis):
    monkeypatch.setattr(api, "_RATE_LIMIT", 3)
    monkeypatch.setattr(api, "_MISS_LIMIT", 2)
    return rl_redis


async def test_supera_el_cupo_en_la_ventana(redis) -> None:
    ip = "203.0.113.1"
    assert not await api._rate_limited("tenant-a", ip)
    assert not await api._rate_limited("tenant-a", ip)
    assert not await api._rate_limited("tenant-a", ip)
    assert await api._rate_limited("tenant-a", ip)  # cuarta supera el tope de 3


async def test_cupo_de_un_tenant_no_afecta_a_otro(redis) -> None:
    ip = "203.0.113.1"
    for _ in range(5):
        await api._rate_limited("tenant-a", ip)
    assert await api._rate_limited("tenant-a", ip)

    # Misma IP, otro tenant: cupo intacto.
    assert not await api._rate_limited("tenant-b", ip)
    assert not await api._rate_limited("tenant-b", ip)
    assert not await api._rate_limited("tenant-b", ip)
    assert await api._rate_limited("tenant-b", ip)


async def test_slug_invalido_no_crea_claves_arbitrarias(redis) -> None:
    await api._rate_limited("../../" + "x" * 500, "203.0.113.1")
    assert redis.keys() == {f"{api._RATE_KEY_PREFIX}:_invalid:203.0.113.1"}


async def test_trafico_valido_no_gasta_el_cupo_de_fallos(redis) -> None:
    """Tenants que comparten la IP de salida (hosting compartido) no se gastan
    el cupo de fallos entre sí con leads legítimos."""
    ip = "203.0.113.1"
    for slug in ("tenant-a", "tenant-b"):
        for _ in range(3):
            assert not await api._rate_limited(slug, ip)
    assert f"{api._MISS_KEY_PREFIX}:{ip}" not in redis.keys()


async def test_slug_conocido_no_lo_corta_el_cupo_de_fallos(redis) -> None:
    """Tenants que comparten la IP de salida: si uno apunta a un slug que ya no
    existe y llena el cupo de fallos, los slugs conocidos siguen pasando."""
    from core.security import rate_limit

    ip = "203.0.113.1"
    await rate_limit.remember(api._known_key("tenant-a"), api._KNOWN_TTL_S, name="t")
    for _ in range(api._MISS_LIMIT):
        await rate_limit.record(api._miss_bucket(ip), name="t")

    assert not await api._rate_limited("tenant-a", ip)
    assert await api._rate_limited("tenant-b", ip)  # no conocido


def test_slug_invalido_nunca_es_conocido() -> None:
    assert api._known_key("../x") is None
    assert api._known_key("Tenant-A") is None


def _request(peer: str, slug: str) -> Request:
    body = json.dumps({"nombre": "Ana", "email": "ana@example.com"}).encode()

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/pack/pack_commerce/leads/{slug}",
            "headers": [(b"content-type", b"application/json")],
            "client": (peer, 40000),
        },
        receive,
    )


async def test_slugs_inexistentes_cortan_por_ip(monkeypatch: pytest.MonkeyPatch, redis) -> None:
    """Un slug al azar por request abre un cupo nuevo cada vez; los 404 suman
    por IP y, con ese cupo lleno, la IP recibe 429 sin resolver nada. Otra IP
    sigue pasando."""
    resolved: list[str] = []

    async def _unknown(slug: str):
        resolved.append(slug)
        return None

    monkeypatch.setattr(api, "resolve_tenant_by_slug", _unknown)

    for i in range(api._MISS_LIMIT):
        slug = f"azar-{i}"
        res = await api.intake_lead(slug, _request("198.51.100.7", slug), "secreto")
        assert res.status_code == 404
    res = await api.intake_lead("azar-otro", _request("198.51.100.7", "azar-otro"), "secreto")
    assert res.status_code == 429
    assert res.body == b'{"error":"rate_limited"}'
    assert len(resolved) == api._MISS_LIMIT  # el 429 no llegó a la base

    res = await api.intake_lead("azar-otro", _request("198.51.100.8", "azar-otro"), "secreto")
    assert res.status_code == 404
