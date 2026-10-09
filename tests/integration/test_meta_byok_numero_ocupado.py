"""BYOK: `phone_number_id` es único GLOBAL (lo asigna Meta).

Dos regresiones reales (tenant `solvershub` en test, 2026-09-24):
1. Un número ya reclamado por OTRO tenant reventaba con `IntegrityError` crudo
   (500 en el wizard; mensaje SQL ilegible en el alta). Ahora da `ByokError`.
2. Eliminar una conexión no liberaba el número/WABA (quedaban colgados de la
   conexión revocada y bloqueaban reclamarlos). Ahora se borran sus assets.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import stub_meta_pair_ok

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def _switch_tenant(app_client, session_cookie: str, tenant_id: uuid.UUID) -> str:
    r = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session_cookie}, data={"tenant_id": str(tenant_id)}
    )
    ac_tenant = next(c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c)
    return f"{session_cookie}; {ac_tenant}"


async def test_numero_ya_de_otro_tenant_da_byok_error_limpio(monkeypatch) -> None:
    """Tenant B intenta un número reclamado por A -> ByokError, no IntegrityError.

    Desde ADR-0095 lo frena la comprobación de reclamo antes de tocar las tablas (el
    unique global de `phone_number_id` queda como segunda barrera): B no crea nada."""
    import core.gateway.meta_byok as byok_mod
    from core.alta_negocio import onboard_tenant
    from core.db.models import MetaConnection, PhoneNumber
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    phone_id = _uniq("phone-dup")

    # Tenant A reclama el número.
    slug_a = _uniq("whdup-a")
    tid_a, _ = await onboard_tenant(name=f"Dup A {slug_a}", slug=slug_a, persona={"tone": "cercano"})
    async with tenant_scope(tid_a):
        async with get_session() as session:
            conn_a = MetaConnection(
                tenant_id=tid_a, channel="whatsapp", environment="sandbox",
                origin="byok", meta_app_id=_uniq("app-a"), status="connected",
            )
            session.add(conn_a)
            await session.flush()
            session.add(
                PhoneNumber(tenant_id=tid_a, connection_id=conn_a.id, phone_number_id=phone_id, registered=True)
            )

    # Tenant B intenta el mismo número.
    slug_b = _uniq("whdup-b")
    tid_b, _ = await onboard_tenant(name=f"Dup B {slug_b}", slug=slug_b, persona={"tone": "cercano"})

    async def _verify_ok(phone_number_id, access_token, app_secret):
        return True, "ok", {"display_phone_number": "+1 555", "verified_name": "Dup"}

    async def _subscribe_ok(*a, **k):
        return True

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _verify_ok)
    stub_meta_pair_ok(monkeypatch)
    monkeypatch.setattr(byok_mod, "_subscribe_waba_to_app", _subscribe_ok)

    with pytest.raises(byok_mod.ByokError) as exc:
        async with tenant_scope(tid_b):
            await byok_mod.save_byok_connection(
                tid_b, None, _uniq("app-b"), "secret-b", phone_id, "access-b", _uniq("waba-b"),
            )
    from core.gateway.meta_asset_claims import CLAIMED_MESSAGE

    assert str(exc.value) == CLAIMED_MESSAGE
    from sqlalchemy import select

    async with tenant_scope(tid_b):
        async with get_session() as session:
            assert (await session.execute(select(MetaConnection))).scalars().all() == []


async def test_eliminar_conexion_libera_numero_y_waba(app_client) -> None:
    """Eliminar (soft-delete) una conexión borra sus PhoneNumber/WabaAccount, para
    que el número/WABA se puedan reclamar de nuevo."""
    from sqlalchemy import select

    from core.alta_negocio import onboard_tenant
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = _uniq("whfree")
    tid, _ = await onboard_tenant(
        name=f"Free {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local"
    )
    phone_id = _uniq("phone-free")
    waba_id = _uniq("waba-free")

    async with tenant_scope(tid):
        async with get_session() as session:
            conn = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox",
                origin="byok", meta_app_id=_uniq("app-free"), status="connected",
            )
            session.add(conn)
            await session.flush()
            waba = WabaAccount(tenant_id=tid, connection_id=conn.id, waba_id=waba_id)
            session.add(waba)
            await session.flush()
            session.add(
                PhoneNumber(
                    tenant_id=tid, connection_id=conn.id, waba_account_id=waba.id,
                    phone_number_id=phone_id, registered=True,
                )
            )
            connection_id = conn.id

    cookie = await _switch_tenant(app_client, await _login(app_client), tid)
    r = await app_client.post(
        f"/admin/conexiones/meta/{connection_id}/eliminar", headers={"Cookie": cookie}
    )
    assert r.status_code == 303

    async with tenant_scope(tid):
        async with get_session() as session:
            phones = (
                await session.execute(
                    select(PhoneNumber).where(PhoneNumber.phone_number_id == phone_id)
                )
            ).scalars().all()
            wabas = (
                await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id))
            ).scalars().all()
            conn_row = await session.get(MetaConnection, connection_id)

    assert phones == [], "el número debe liberarse para poder reclamarlo de nuevo"
    assert wabas == [], "el WABA debe liberarse (si no, resolve_tenant_for_waba rutea mal)"
    assert conn_row is not None and conn_row.deleted_at is not None, "la conexión queda soft-deleted"
