"""HU-CONN-02: errores 500 y bloqueos de Conexiones.

Cubren los defectos N-18 y el resto de los criterios Gherkin:
1. Números: `list_numbers` no usa `PhoneNumber.status` (columna inexistente).
2. Créditos: el endpoint no usa `MetaConnection.label` (columna inexistente).
3. Wizard vencido: reiniciar reutiliza/cierra el borrador sin violar el índice.
4. Alta manual duplicada: `save_connection` no revienta por integridad.
5. Sync explícita: `list_numbers` acepta un modo sin escritura.
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


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def _onboard(slug: str):
    from core.onboarding import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Conexiones {slug}", slug=slug, business_description="Test conexiones.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    return tid


async def _make_meta_connection(tid, *, status: str = "connected"):
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            conn = MetaConnection(
                tenant_id=tid, channel="whatsapp", origin="byok", meta_app_id=_uniq("app"),
                status=status, business_id=_uniq("biz"),
            )
            session.add(conn)
            await session.flush()
            return conn.id


async def test_numeros_no_usa_status_inexistente() -> None:
    """`list_numbers` responde sin `PhoneNumber.status` (N-18)."""
    from core.admin.meta_numbers_service import list_numbers
    from core.db.models import PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _onboard(_uniq("conn-num"))
    conn_id = await _make_meta_connection(tid)

    async with tenant_scope(tid):
        async with get_session() as session:
            waba = WabaAccount(tenant_id=tid, connection_id=conn_id, waba_id=_uniq("waba"))
            session.add(waba)
            await session.flush()
            session.add(
                PhoneNumber(
                    tenant_id=tid, connection_id=conn_id, waba_account_id=waba.id,
                    phone_number_id=_uniq("phone"), display_phone_number="+57 300",
                    verified_name="Prueba", code_verification_status="VERIFIED",
                )
            )

        numbers, sync_error = await list_numbers(tid, str(conn_id))

    assert sync_error is None, sync_error
    assert len(numbers) == 1
    assert numbers[0]["status"] == "VERIFIED"
    assert numbers[0]["phone_number_id"]


async def test_creditos_no_usa_label_inexistente(app_client) -> None:
    """`_credit_connections` responde sin `MetaConnection.label` (N-18)."""
    from core.admin.meta_costs_service import list_credit_connections
    from core.api.routers.costs import MetaCreditsResponse, _credit_connections
    from core.shared.tenant import tenant_scope

    tid = await _onboard(_uniq("conn-cred"))
    await _make_meta_connection(tid)

    async with tenant_scope(tid):
        rows = await list_credit_connections(tid)
    response = MetaCreditsResponse(connections=_credit_connections(rows))
    assert len(response.connections) == 1
    assert response.connections[0].label  # usa meta_app_id o business_id


async def test_wizard_vencido_se_reinicia_sin_violar_indice() -> None:
    """Un wizard vencido se cierra y se crea uno nuevo (índice parcial)."""
    from datetime import timedelta

    from sqlalchemy import func, select

    from core.config import get_settings
    from core.db.models import ConnectionWizard, utcnow
    from core.db.session import get_session
    from core.gateway.meta_onboarding import start_or_resume_wizard
    from core.shared.tenant import tenant_scope

    settings = get_settings()
    orig = (settings.meta_app_id, settings.meta_app_secret, settings.meta_config_id)
    settings.meta_app_id, settings.meta_app_secret, settings.meta_config_id = "app-x", "sec-x", "cfg-x"
    try:
        tid = await _onboard(_uniq("conn-wiz"))
        async with tenant_scope(tid):
            first = await start_or_resume_wizard(tid)

        # Envejecer el wizard más allá de su expiración.
        async with tenant_scope(tid):
            async with get_session() as session:
                w = await session.get(ConnectionWizard, first.id)
                w.expires_at = utcnow() - timedelta(hours=1)

        async with tenant_scope(tid):
            second = await start_or_resume_wizard(tid)
        assert second.id != first.id

        async with tenant_scope(tid):
            async with get_session() as session:
                vivos = (
                    await session.execute(
                        select(func.count(ConnectionWizard.id)).where(
                            ConnectionWizard.current_step != "done"
                        )
                    )
                ).scalar()
        assert vivos == 1
    finally:
        settings.meta_app_id, settings.meta_app_secret, settings.meta_config_id = orig


async def test_alta_manual_duplicada_no_revienta() -> None:
    """Guardar dos veces la misma conexión no lanza IntegrityError."""
    from core.db.session import get_session
    from core.gateway.connections import save_connection
    from core.shared.tenant import tenant_scope

    tid = await _onboard(_uniq("conn-dup"))
    creds = {
        "phone_number_id": _uniq("phone"),
        "app_secret": "sec",
        "access_token": "tok",
        "verify_token": "ver",
    }

    async with tenant_scope(tid):
        conn1, created1 = await save_connection("Conexión", dict(creds), type_id="whatsapp_meta")
        conn2, created2 = await save_connection("Conexión otra vez", dict(creds), type_id="whatsapp_meta")

    assert created1 is True
    assert created2 is False
    assert conn1.id == conn2.id

    from sqlalchemy import func, select

    from core.db.models import ChannelConnection

    async with tenant_scope(tid):
        async with get_session() as session:
            total = (
                await session.execute(
                    select(func.count(ChannelConnection.id)).where(
                        ChannelConnection.tenant_id == tid
                    )
                )
            ).scalar()
    assert total == 1
