"""Intake público de leads del core (ADR-0193 §5, HU-AOS-70 parte 3).

`POST /api/public/leads/{slug}` con header `X-Intake-Secret`, y el alias `POST /api/pack/pack_commerce/leads/{slug}`
de la ruta vieja. La config es la de Captar leads (`lead_settings`), sin depender de commerce.
Requiere el stack real (Postgres+Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
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
    from core.leads import intake as api
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
    tenant_id, enabled: bool, turnstile_secret: str | None = None, origins: list[str] | None = None
) -> None:
    """Config de Captar leads del negocio (`lead_settings`), no la de commerce."""
    from core.db.session import get_session
    from core.leads import config as config_leads
    from core.shared.tenant import tenant_scope

    config = config_leads.ConfigCaptarLeads(
        lead_intake_enabled=enabled,
        lead_intake_secret=SECRET,
        lead_intake_turnstile=turnstile_secret or "",
        lead_intake_allowed_origins=origins or [],
    )
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await config_leads.guardar(session, tenant_id, config)


async def _cleanup(tenant_id) -> None:
    from datetime import UTC, datetime

    from sqlalchemy import update

    from core.db.models import Lead
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    # El lead es del core (ADR-0193): sin borrado físico, se retira el de la corrida anterior.
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            await session.execute(
                update(Lead)
                .where(Lead.idempotency_key == IDEM, Lead.deleted_at.is_(None))
                .values(deleted_at=datetime.now(UTC))
            )


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
    r = await app_client.post(f"/api/public/leads/{SLUG}", json=_payload())
    assert r.status_code == 401, r.text

    # secreto incorrecto → 401
    r = await app_client.post(
        f"/api/public/leads/{SLUG}",
        headers={"X-Intake-Secret": "nope"},
        json=_payload(),
    )
    assert r.status_code == 401

    # secreto correcto → 200 {ok:true}
    r = await app_client.post(
        f"/api/public/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


async def test_intake_crea_y_es_idempotente(app_client, moto_setup) -> None:
    from sqlalchemy import func, select

    from core.db.models import Customer, Lead, MemoryFact
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _t1, moto = moto_setup
    await _configure_intake(moto, enabled=True)
    await _cleanup(moto)

    for _ in range(2):
        r = await app_client.post(
            f"/api/public/leads/{SLUG}",
            headers={"X-Intake-Secret": SECRET},
            json=_payload(),
        )
        assert r.status_code == 200

    async with tenant_scope(moto):
        async with get_session() as session:
            n = (
                await session.execute(
                    select(func.count())
                    .select_from(Lead)
                    .where(Lead.idempotency_key == IDEM, Lead.deleted_at.is_(None))
                )
            ).scalar()
            lead = (
                await session.execute(select(Lead).where(Lead.idempotency_key == IDEM, Lead.deleted_at.is_(None)))
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
    assert lead.origen == "landing"


async def test_intake_deshabilitado_y_validacion(app_client, moto_setup) -> None:
    _t1, moto = moto_setup

    # slug desconocido → 404
    r = await app_client.post(
        "/api/public/leads/no-existe",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 404

    # payload inválido → 422
    r = await app_client.post(
        f"/api/public/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json={"nombre": "", "email": "", "whatsapp": ""},
    )
    assert r.status_code == 422

    # intake deshabilitado → 403
    await _configure_intake(moto, enabled=False)
    r = await app_client.post(
        f"/api/public/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 403


async def test_intake_config(app_client, moto_setup) -> None:
    """Config no sensible para el formulario alojado (frame-ancestors)."""
    _t1, moto = moto_setup
    await _configure_intake(moto, enabled=True)

    r = await app_client.get(f"/api/public/leads/intake-config/{SLUG}")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["allowed_origins"] == []
    assert body["turnstile"] is False

    # slug desconocido → 404
    r = await app_client.get("/api/public/leads/intake-config/no-existe")
    assert r.status_code == 404


async def test_intake_turnstile(app_client, moto_setup, monkeypatch) -> None:
    """Turnstile: se exige y valida solo si el tenant tiene secret configurado."""
    from core.leads import intake as intake_api

    _t1, moto = moto_setup
    await _cleanup(moto)

    # 1) Tenant CON turnstile: token ausente → 403 (no llama a Cloudflare).
    await _configure_intake(moto, enabled=True, turnstile_secret="tt-secret")
    r = await app_client.post(
        f"/api/public/leads/{SLUG}",
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
        f"/api/public/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json={**_payload(), "turnstile_token": "bad"},
    )
    assert r.status_code == 403

    # 3) Token válido (stub) → 200 y crea el lead.
    async def _ok(token: str, secret: str, ip):
        return bool(token)

    monkeypatch.setattr(intake_api, "_verify_turnstile", _ok)
    r = await app_client.post(
        f"/api/public/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json={**_payload(), "turnstile_token": "good"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    # 4) Sin turnstile configurado → no se exige token (200).
    await _configure_intake(moto, enabled=True, turnstile_secret="")
    r = await app_client.post(
        f"/api/public/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 200


async def test_intake_rate_limit_por_tenant(app_client, moto_setup, monkeypatch) -> None:
    """429 `rate_limited` al pasar el cupo; el cupo es por tenant y la clave
    en Redis vence sola (TTL = ventana)."""
    from core.leads import intake as intake_api
    from core.memory.working import get_redis

    _t1, moto = moto_setup
    await _configure_intake(moto, enabled=True)
    await _cleanup(moto)
    monkeypatch.setattr(intake_api, "_RATE_LIMIT", 2)

    for _ in range(2):
        r = await app_client.post(
            f"/api/public/leads/{SLUG}",
            headers={"X-Intake-Secret": SECRET},
            json=_payload(),
        )
        assert r.status_code == 200, r.text

    r = await app_client.post(
        f"/api/public/leads/{SLUG}",
        headers={"X-Intake-Secret": SECRET},
        json=_payload(),
    )
    assert r.status_code == 429
    assert r.json() == {"error": "rate_limited"}

    # Misma IP, otro slug: no hereda el cupo agotado (llega a resolver el tenant).
    r = await app_client.post(
        "/api/public/leads/no-existe",
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
            f"/api/public/leads/{SLUG}",
            headers={"X-Intake-Secret": SECRET},
            json=_payload(),
        )
        assert r.status_code == 429
    assert await get_redis().zcard(keys[0]) == intake_api._RATE_LIMIT + 1


async def test_intake_slugs_inexistentes_cortan_por_ip(app_client, moto_setup, monkeypatch) -> None:
    """Contra Redis real: los 404 suman en el cupo de fallos de la IP y, lleno,
    la IP recibe 429 antes de resolver el tenant, salvo en slugs que ya se
    resolvieron (conocidos)."""
    from core.leads import intake as intake_api
    from core.memory.working import get_redis

    _t1, moto = moto_setup
    await _configure_intake(moto, enabled=True)
    await _cleanup(moto)
    monkeypatch.setattr(intake_api, "_MISS_LIMIT", 2)

    async def _post(slug: str):
        return await app_client.post(
            f"/api/public/leads/{slug}",
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


async def _negocio_sin_comercio():
    from uuid import uuid4

    from core.alta_negocio import onboard_tenant

    slug = f"lsc-{uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(
        name=f"Sin comercio {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=[], sembrar_ejemplos=False,
    )
    return tid, slug


async def _leads_vivos(tenant_id) -> list:
    from sqlalchemy import select

    from core.db.models import Lead
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return list(
                (await session.execute(select(Lead).where(Lead.deleted_at.is_(None)))).scalars().all()
            )


async def test_intake_sin_comercio_por_la_ruta_nueva_y_el_alias(app_client) -> None:
    """Gherkin «Intake sin comercio» y «Ruta vieja como alias»: un negocio sin `pack_commerce` recibe el lead por las
    dos rutas, deduplicado en un solo cliente y un solo lead con origen `landing`."""
    from core.agents.spec import enabled_packs, load_active_spec
    from core.shared.tenant import tenant_scope

    tid, slug = await _negocio_sin_comercio()
    async with tenant_scope(tid):
        assert "pack_commerce" not in enabled_packs(await load_active_spec(tid))

    # Sin config de Captar leads: el intake está apagado (403), y la config pública lo dice.
    r = await app_client.post(f"/api/public/leads/{slug}", headers={"X-Intake-Secret": SECRET}, json=_payload())
    assert r.status_code == 403 and r.json() == {"error": "intake_disabled"}
    r = await app_client.get(f"/api/public/leads/intake-config/{slug}")
    assert r.json() == {"enabled": False, "allowed_origins": [], "turnstile": False}

    await _configure_intake(tid, enabled=True, turnstile_secret="tt", origins=["https://www.ejemplo.co"])
    for prefijo in ("/api/public/leads", "/api/pack/pack_commerce"):
        r = await app_client.get(f"{prefijo}/intake-config/{slug}")
        assert r.status_code == 200
        assert r.json() == {"enabled": True, "allowed_origins": ["https://www.ejemplo.co"], "turnstile": True}

    await _configure_intake(tid, enabled=True)
    r = await app_client.post(f"/api/public/leads/{slug}", headers={"X-Intake-Secret": SECRET}, json=_payload())
    assert r.status_code == 200, r.text
    r = await app_client.post(
        f"/api/pack/pack_commerce/leads/{slug}", headers={"X-Intake-Secret": SECRET}, json=_payload("Otro nombre")
    )
    assert r.status_code == 200, r.text
    # Alias con el secreto equivocado: 401 igual que la ruta nueva.
    r = await app_client.post(f"/api/pack/pack_commerce/leads/{slug}", headers={"X-Intake-Secret": "x"}, json=_payload())
    assert r.status_code == 401

    [lead] = await _leads_vivos(tid)
    assert lead.origen == "landing" and lead.email == EMAIL and lead.idempotency_key == IDEM


async def test_intake_no_cruza_negocios(app_client, moto_setup) -> None:
    """Gherkin «Aislamiento»: el secreto de un negocio no abre el intake de otro y el lead queda solo en el suyo."""
    _t1, moto = moto_setup
    await _configure_intake(moto, enabled=True)
    await _cleanup(moto)
    otro, slug_otro = await _negocio_sin_comercio()
    from core.db.session import get_session
    from core.leads import config as config_leads
    from core.shared.tenant import tenant_scope

    async with tenant_scope(otro):
        async with get_session() as session:
            await config_leads.guardar(
                session,
                otro,
                config_leads.ConfigCaptarLeads(lead_intake_enabled=True, lead_intake_secret="secreto-del-otro"),
            )

    r = await app_client.post(f"/api/public/leads/{slug_otro}", headers={"X-Intake-Secret": SECRET}, json=_payload())
    assert r.status_code == 401
    r = await app_client.post(f"/api/public/leads/{SLUG}", headers={"X-Intake-Secret": SECRET}, json=_payload())
    assert r.status_code == 200
    assert await _leads_vivos(otro) == []
    assert [lead.idempotency_key for lead in await _leads_vivos(moto) if lead.idempotency_key == IDEM] == [IDEM]
