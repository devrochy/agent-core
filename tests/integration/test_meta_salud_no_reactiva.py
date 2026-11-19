"""Diagnosticar y el cron de salud no reactivan una conexión desactivada (ADR-0101).

Desde ADR-0101 una conexión `degraded`/`error` sigue prestando su App Secret y su verify
token. Si "Diagnosticar" o el cron pasaran una `revoked`/`draft` a esos estados, la
desactivación a mano dejaría de cortar la entrada del negocio.
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

CHECK_FALLIDO = {"phone_quality": {"ok": False}}  # cualquier check fallido → `degraded`


async def _conexion(status: str) -> tuple[uuid.UUID, uuid.UUID]:
    from core.alta_negocio import onboard_tenant
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = f"salud-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(name=f"Salud {slug}", slug=slug, persona={"tone": "cercano"})
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok",
                meta_app_id=f"app-{slug}", status=status,
            )
            session.add(connection)
            await session.flush()
            return tid, connection.id


async def _estado(tid: uuid.UUID, cid: uuid.UUID) -> tuple[str, object]:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = await session.get(MetaConnection, cid)
            return connection.status, connection.health


@pytest.fixture
def checks_fallidos(monkeypatch):
    async def fake_checks(_connection, _tenant_id):
        return CHECK_FALLIDO

    monkeypatch.setattr("core.gateway.meta_health.run_health_checks", fake_checks)
    monkeypatch.setattr("core.tasks.meta_health_check.run_health_checks", fake_checks)


@pytest.mark.parametrize("status", ["revoked", "draft"])
async def test_diagnosticar_no_reactiva_una_conexion_desactivada(checks_fallidos, status) -> None:
    from core.admin.meta_health_service import diagnose_connection
    from core.shared.tenant import tenant_scope

    tid, cid = await _conexion(status)

    async with tenant_scope(tid):
        assert await diagnose_connection(tid, str(cid)) is None

    estado, health = await _estado(tid, cid)
    assert estado == status
    assert health == CHECK_FALLIDO  # el diagnóstico sí queda guardado


async def test_diagnosticar_una_conexion_activa_si_cambia_su_estado(checks_fallidos) -> None:
    from core.admin.meta_health_service import diagnose_connection
    from core.shared.tenant import tenant_scope

    tid, cid = await _conexion("connected")

    async with tenant_scope(tid):
        assert await diagnose_connection(tid, str(cid)) is None

    assert (await _estado(tid, cid))[0] == "degraded"


async def test_el_cron_no_pisa_una_conexion_desactivada_durante_la_corrida(checks_fallidos) -> None:
    """El cron arma la lista al empezar; si la desactivan antes de que le toque, no la revive."""
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tasks.meta_health_check import _check_one

    tid, cid = await _conexion("connected")
    async with tenant_scope(tid):
        async with get_session() as session:
            listada = await session.get(MetaConnection, cid)
        async with get_session() as session:
            (await session.get(MetaConnection, cid)).status = "revoked"

    await _check_one(listada)

    assert (await _estado(tid, cid))[0] == "revoked"
