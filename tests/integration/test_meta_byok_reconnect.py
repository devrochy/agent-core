"""BYOK: reconectar una app tras "eliminar" su conexión (soft-delete).

Regresión: el unique de `meta_connections` pasó a ser PARCIAL (`where deleted_at
is null`, migración 0032) justamente para poder reconectar la misma app creando
una fila nueva. El lookup de `save_byok_connection` no filtraba `deleted_at`, así
que resucitaba la fila borrada sin limpiar `deleted_at`: quedaba "pending" pero
invisible al resolver del webhook -> el self-check daba 403.
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


async def test_byok_reconecta_tras_soft_delete_crea_conexion_nueva(app_client, monkeypatch) -> None:
    import core.gateway.meta_byok as byok_mod
    from core.db.models import MetaConnection, PhoneNumber, utcnow
    from core.db.session import get_session
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = f"whreconn-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(name=f"Reconnect {slug}", slug=slug, persona={"tone": "cercano"})

    app_id = f"app-reconn-{uuid.uuid4().hex[:6]}"
    environment = "sandbox"
    # `phone_number_id` ya existente (único global): el reconnect debe reutilizar
    # la fila, no insertar un duplicado.
    phone_id = f"phone-{uuid.uuid4().hex[:8]}"

    # 1) Conexión vieja, ya "eliminada" (soft-delete) con su token y su número.
    async with tenant_scope(tid):
        async with get_session() as session:
            old = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment=environment, origin="byok", meta_app_id=app_id,
                status="revoked", deleted_at=utcnow(),
            )
            session.add(old)
            await session.flush()
            old_id = old.id
            session.add(
                PhoneNumber(tenant_id=tid, connection_id=old_id, phone_number_id=phone_id, registered=True)
            )
        await byok_mod.meta_token_store.put_token(
            old_id, byok_mod.WEBHOOK_VERIFY_TOKEN_KIND, app_id, "tok-viejo", tenant_id=tid
        )

    # 2) Sin red: mockear Graph + subscribe (lo que probamos es la persistencia).
    async def _verify_ok(phone_number_id, access_token, app_secret):
        return True, "ok", {"display_phone_number": "+1 555", "verified_name": "Reconnect"}

    async def _subscribe_ok(*a, **k):
        return True

    async def _upsert_ok(*a, **k):
        return None

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _verify_ok)
    stub_meta_pair_ok(monkeypatch)
    monkeypatch.setattr(byok_mod, "_subscribe_waba_to_app", _subscribe_ok)
    monkeypatch.setattr("core.gateway.meta_subscriptions.upsert_app_subscription", _upsert_ok)

    # 3) Reconectar la MISMA app/entorno y el MISMO número.
    async with tenant_scope(tid):
        conn = await byok_mod.save_byok_connection(
            tid, None, app_id, "secret-nuevo", phone_id, "access-nuevo", f"waba-{uuid.uuid4().hex[:8]}",
            environment=environment, verify_token="tok-nuevo",
        )

    # 4) Debe ser una fila NUEVA (no la borrada resucitada).
    assert conn.id != old_id, "debe crear una conexión nueva, no reutilizar la soft-deleted"
    assert conn.deleted_at is None
    assert conn.status == "connected"

    # 5) El número NO se duplicó: la MISMA fila quedó reasignada a la conexión nueva.
    from sqlalchemy import select
    async with tenant_scope(tid):
        async with get_session() as session:
            rows = (
                await session.execute(select(PhoneNumber).where(PhoneNumber.phone_number_id == phone_id))
            ).scalars().all()
    assert len(rows) == 1, "phone_number_id es único global: no debe duplicarse"
    assert rows[0].connection_id == conn.id, "el número debe reasignarse a la conexión nueva"

    # 5) El handshake acepta el token nuevo y ya NO el viejo.
    r = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "tok-nuevo", "hub.challenge": "RC"},
    )
    assert r.status_code == 200 and r.text == "RC"

    r2 = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "tok-viejo", "hub.challenge": "RC"},
    )
    assert r2.status_code == 403

    # 6) Outbound: el sender encuentra el access token de ESTA conexión BYOK
    # (`cloud_api_access_token`, subject=meta_app_id) en vez de caer al `.env`.
    from core.gateway.sender import _resolve_meta_connection_credentials

    async with tenant_scope(tid):
        outbound = await _resolve_meta_connection_credentials(conn.id, tid)
    assert outbound == {"phone_number_id": phone_id, "access_token": "access-nuevo"}
