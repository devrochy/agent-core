"""Crons de Meta: enumeración cross-tenant vía funciones SECURITY DEFINER.

Regresión: los crons enumeraban conexiones/WABAs sin `tenant_scope` y, con RLS
fail-closed, obtenían 0 filas -> corrían como no-op silencioso. Ahora enumeran
ids con `admin_*_ids()` (migración 0033) e hidratan bajo el scope del tenant.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _seed(slug: str):
    from core.db.models import MetaConnection, WabaAccount
    from core.db.session import get_session
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    tid, _ = await onboard_tenant(name=f"AdminEnum {slug}", slug=slug, persona={"tone": "cercano"})
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox",
                meta_app_id=f"app-{slug}", status="connected",
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id
            session.add(WabaAccount(tenant_id=tid, connection_id=connection_id, waba_id=f"waba-{slug}"))
    return tid, connection_id


async def test_admin_enum_ve_cross_tenant_mientras_rls_no() -> None:
    from sqlalchemy import func, select

    from core.db.admin_enum import admin_connected_waba_ids, admin_meta_connection_ids
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = f"admenum-{uuid.uuid4().hex[:6]}"
    tid, connection_id = await _seed(slug)

    # La función SECURITY DEFINER ve el asset del tenant sin contexto...
    conn_pairs = await admin_meta_connection_ids(["connected"])
    assert (connection_id, tid) in conn_pairs

    waba_pairs = await admin_connected_waba_ids()
    assert any(t == tid for _, t in waba_pairs)

    # ...mientras un select plano SIN tenant_scope devuelve 0 (RLS fail-closed).
    async with tenant_scope(None):
        async with get_session() as session:
            n = (await session.execute(select(func.count(MetaConnection.id)))).scalar()
    assert n == 0, "RLS debe seguir bloqueando el select plano (fail-closed)"


async def test_crons_de_meta_encuentran_el_asset_del_tenant() -> None:
    from core.tasks.message_template_sync import _connected_wabas
    from core.tasks.meta_health_check import _connections_to_check

    slug = f"admcron-{uuid.uuid4().hex[:6]}"
    tid, connection_id = await _seed(slug)

    connections = await _connections_to_check()
    assert any(c.id == connection_id and c.tenant_id == tid for c in connections)

    wabas = await _connected_wabas()
    assert any(connection.id == connection_id and connection.tenant_id == tid for _waba, connection in wabas)


async def test_admin_meta_connection_ids_excluye_soft_deleted() -> None:
    from core.db.admin_enum import admin_meta_connection_ids
    from core.db.models import MetaConnection, utcnow
    from core.db.session import get_session
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = f"admdel-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(name=f"AdminEnumDel {slug}", slug=slug, persona={"tone": "cercano"})
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox",
                meta_app_id=f"app-{slug}", status="connected", deleted_at=utcnow(),
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id

    pairs = await admin_meta_connection_ids(["connected"])
    assert (connection_id, tid) not in pairs, "una conexión soft-deleted no debe enumerarse"
