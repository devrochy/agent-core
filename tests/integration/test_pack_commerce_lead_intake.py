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
