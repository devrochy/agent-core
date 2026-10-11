"""Intake público de leads del pack de comercio (ADR-0057/ADR-0055 superseded).

`POST /api/pack/pack_commerce/leads/{slug}` con header `X-Intake-Secret`.
Requiere el stack real (Postgres+Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

SLUG = "tenant-moto"
SECRET = "it-intake-secret"
EMAIL = "lead-intake@test.local"
IDEM = f"intake:{EMAIL}"


@pytest.fixture(autouse=True)
async def _reset_rate_limit():
    """El rate limit vive en Redis (no en el proceso): sin limpiar, dos corridas
    seguidas dentro de la ventana comparten cupo."""
    from pack_commerce import api

    from core.memory.working import get_redis

    r = get_redis()
    patterns = [f"{api._RATE_KEY_PREFIX}:{slug}:*" for slug in (SLUG, "no-existe", "no-existe-2")]
    patterns.append(f"{api._MISS_KEY_PREFIX}:*")
    patterns.append(f"{api._KNOWN_KEY_PREFIX}:{SLUG}")
    keys = [k for pattern in patterns async for k in r.scan_iter(match=pattern)]
    if keys:
        await r.delete(*keys)
    yield


async def _configure_intake(
    tenant_id, enabled: bool, turnstile_secret: str | None = None
) -> None:
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    values: dict = {"lead_intake_enabled": enabled, "lead_intake_secret": SECRET}
    if turnstile_secret is not None:
        values["lead_intake_turnstile"] = turnstile_secret
    async with tenant_scope(tenant_id):
        await plugin_manager.save_config("pack_commerce", values)


async def _cleanup(tenant_id) -> None:
    from pack_commerce.models import Lead
    from sqlalchemy import delete

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await session.execute(delete(Lead).where(Lead.idempotency_key == IDEM))


def _payload(nombre: str = "Lead Intake") -> dict:
    return {
        "nombre": nombre,
        "email": EMAIL,
        "whatsapp": "+15550001111",
        "negocio": "Otro",
        "optin": True,
        "origen": "test",
    }


async def test_intake_valida_secreto(app_client, moto_setup) -> None:
    _t1, moto = moto_setup
    await _configure_intake(moto, enabled=True)
    await _cleanup(moto)

    # sin secreto → 401
    r = await app_client.post(f"/api/pack/pack_commerce/leads/{SLUG}", json=_payload())
    assert r.status_code == 401, r.text

    # secreto incorrecto → 401
    r = await app_client.post(
        f"/api/pack/pack_commerce/leads/{SLUG}",
        headers={"X-Intake-Secret": "nope"},
        json=_payload(),
    )
    assert r.status_code == 401

    # secreto correcto → 200 {ok:true}
    r = await app_client.post(
        f"/api/pack/pack_commerce/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


async def test_intake_crea_y_es_idempotente(app_client, moto_setup) -> None:
    from pack_commerce.models import Lead
    from sqlalchemy import func, select

    from core.db.models import Customer, MemoryFact
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _t1, moto = moto_setup
    await _configure_intake(moto, enabled=True)
    await _cleanup(moto)

    for _ in range(2):
        r = await app_client.post(
            f"/api/pack/pack_commerce/leads/{SLUG}",
            headers={"X-Intake-Secret": SECRET},
            json=_payload(),
        )
        assert r.status_code == 200

    async with tenant_scope(moto):
        async with get_session() as session:
            n = (
                await session.execute(
                    select(func.count()).select_from(Lead).where(Lead.idempotency_key == IDEM)
                )
            ).scalar()
            lead = (
                await session.execute(select(Lead).where(Lead.idempotency_key == IDEM))
            ).scalar_one()
            # El perfil del cliente nuevo es proyección de sus hechos (ADR-0114 §3, HU-AOS-36).
            customer = await session.get(Customer, lead.customer_id)
            hechos = (
                await session.execute(
                    select(MemoryFact.key, MemoryFact.source).where(
                        MemoryFact.customer_id == lead.customer_id,
                        MemoryFact.superseded_at.is_(None),
                        MemoryFact.deleted_at.is_(None),
                    )
                )
            ).all()
    assert customer is not None
    assert customer.attributes["negocio"] == "Otro" and customer.attributes["optin"] is True
    assert {k for k, _ in hechos} == set(customer.attributes)
    assert {s for _, s in hechos} == {"import"}
    assert n == 1, "el intake debe ser idempotente (mismo email/phone)"
    assert lead.status == "nuevo"
    assert lead.email == EMAIL


async def test_intake_deshabilitado_y_validacion(app_client, moto_setup) -> None:
    _t1, moto = moto_setup

    # slug desconocido → 404
    r = await app_client.post(
        "/api/pack/pack_commerce/leads/no-existe",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 404

    # payload inválido → 422
    r = await app_client.post(
        f"/api/pack/pack_commerce/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json={"nombre": "", "email": "", "whatsapp": ""},
    )
    assert r.status_code == 422

    # intake deshabilitado → 403
    await _configure_intake(moto, enabled=False)
    r = await app_client.post(
        f"/api/pack/pack_commerce/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 403


async def test_intake_config(app_client, moto_setup) -> None:
    """Config no sensible para el formulario alojado (frame-ancestors)."""
    _t1, moto = moto_setup
    await _configure_intake(moto, enabled=True)

    r = await app_client.get(f"/api/pack/pack_commerce/intake-config/{SLUG}")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["allowed_origins"] == []
    assert body["turnstile"] is False

    # slug desconocido → 404
    r = await app_client.get("/api/pack/pack_commerce/intake-config/no-existe")
    assert r.status_code == 404


async def test_intake_turnstile(app_client, moto_setup, monkeypatch) -> None:
    """Turnstile: se exige y valida solo si el tenant tiene secret configurado."""
    from pack_commerce import api as intake_api

    _t1, moto = moto_setup
    await _cleanup(moto)

    # 1) Tenant CON turnstile: token ausente → 403 (no llama a Cloudflare).
    await _configure_intake(moto, enabled=True, turnstile_secret="tt-secret")
    r = await app_client.post(
        f"/api/pack/pack_commerce/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 403
    assert r.json()["error"] == "turnstile_failed"

    # 2) Token inválido (siteverify dice success=false) → 403.
    async def _fail(token: str, secret: str, ip):
        return False

    monkeypatch.setattr(intake_api, "_verify_turnstile", _fail)
    r = await app_client.post(
        f"/api/pack/pack_commerce/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json={**_payload(), "turnstile_token": "bad"},
    )
    assert r.status_code == 403

    # 3) Token válido (stub) → 200 y crea el lead.
    async def _ok(token: str, secret: str, ip):
        return bool(token)

    monkeypatch.setattr(intake_api, "_verify_turnstile", _ok)
    r = await app_client.post(
        f"/api/pack/pack_commerce/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json={**_payload(), "turnstile_token": "good"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # 4) Sin turnstile configurado → no se exige token (200).
    await _configure_intake(moto, enabled=True, turnstile_secret="")
    r = await app_client.post(
        f"/api/pack/pack_commerce/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 200


async def test_intake_rate_limit_por_tenant(app_client, moto_setup, monkeypatch) -> None:
    """429 `rate_limited` al pasar el cupo; el cupo es por tenant y la clave
    en Redis vence sola (TTL = ventana)."""
    from pack_commerce import api as intake_api

    from core.memory.working import get_redis

    _t1, moto = moto_setup
    await _configure_intake(moto, enabled=True)
    await _cleanup(moto)
    monkeypatch.setattr(intake_api, "_RATE_LIMIT", 2)

    for _ in range(2):
        r = await app_client.post(
            f"/api/pack/pack_commerce/leads/{SLUG}",
            headers={"X-Intake-Secret": SECRET},
            json=_payload(),
        )
        assert r.status_code == 200, r.text

    r = await app_client.post(
        f"/api/pack/pack_commerce/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 429
    assert r.json() == {"error": "rate_limited"}

    # Misma IP, otro slug: no hereda el cupo agotado (llega a resolver el tenant).
    r = await app_client.post(
        "/api/pack/pack_commerce/leads/no-existe",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 404

    keys = [k async for k in get_redis().scan_iter(match=f"{intake_api._RATE_KEY_PREFIX}:{SLUG}:*")]
    assert len(keys) == 1
    ttl = await get_redis().ttl(keys[0])
    assert 0 < ttl <= intake_api._RATE_WINDOW_S

    # Seguir insistiendo no agranda el set: guarda como mucho el cupo + 1.
    for _ in range(5):
        r = await app_client.post(
            f"/api/pack/pack_commerce/leads/{SLUG}",
            headers={"X-Intake-Secret": SECRET},
            json=_payload(),
        )
        assert r.status_code == 429
    assert await get_redis().zcard(keys[0]) == intake_api._RATE_LIMIT + 1


async def test_intake_slugs_inexistentes_cortan_por_ip(app_client, moto_setup, monkeypatch) -> None:
    """Contra Redis real: los 404 suman en el cupo de fallos de la IP y, lleno,
    la IP recibe 429 antes de resolver el tenant, salvo en slugs que ya se
    resolvieron (conocidos)."""
    from pack_commerce import api as intake_api

    from core.memory.working import get_redis

    _t1, moto = moto_setup
    await _configure_intake(moto, enabled=True)
    await _cleanup(moto)
    monkeypatch.setattr(intake_api, "_MISS_LIMIT", 2)

    async def _post(slug: str):
        return await app_client.post(
            f"/api/pack/pack_commerce/leads/{slug}",
            headers={"X-Intake-Secret": SECRET},
            json=_payload(),
        )

    for slug in ("no-existe", "no-existe-2"):
        r = await _post(slug)
        assert r.status_code == 404, r.text

    keys = [k async for k in get_redis().scan_iter(match=f"{intake_api._MISS_KEY_PREFIX}:*")]
    assert len(keys) == 1
    assert 0 < await get_redis().ttl(keys[0]) <= intake_api._MISS_WINDOW_S

    # Slug real pero todavía no conocido: el cupo de fallos lo corta.
    r = await _post(SLUG)
    assert r.status_code == 429
    assert r.json() == {"error": "rate_limited"}

    # Una vez resuelto, queda conocido y los fallos de la IP ya no lo cortan.
    await get_redis().delete(keys[0])
    r = await _post(SLUG)
    assert r.status_code == 200, r.text
    known = f"{intake_api._KNOWN_KEY_PREFIX}:{SLUG}"
    assert 0 < await get_redis().ttl(known) <= intake_api._KNOWN_TTL_S
    for slug in ("no-existe", "no-existe-2"):
        assert (await _post(slug)).status_code == 404
    r = await _post(SLUG)
    assert r.status_code == 200, r.text
    assert (await _post("no-existe")).status_code == 429
