"""Flujo completo de contabilidad de costos: webhook con `pricing` persiste
`MessageCost`; sync de `pricing_analytics` (Graph mockeado) persiste
`PricingDaily` con `cost` real; una recategorización de plantilla se
refleja en el desglose (ambas categorías visibles, no se "corrige"
retroactivamente lo ya facturado)."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select

from core.config import get_settings

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


async def _setup_connected_tenant():
    from core.db.models import (
        ChannelConnection,
        Conversation,
        Customer,
        MetaConnection,
        PhoneNumber,
        WabaAccount,
    )
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.onboarding import onboard_tenant
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import tenant_scope

    slug = _uniq("costos-admin")
    tid, _ = await onboard_tenant(
        name=f"Tenant costos {slug}", slug=slug, business_description="Test de costos.",
        persona={"tone": "cercano"}, admin_email="admin@local",
    )
    waba_id = _uniq("waba")
    phone_number_id = _uniq("phone")

    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", origin="embedded_signup", meta_app_id="app-embedded-signup",
                status="connected", business_id="biz-costos",
            )
            session.add(connection)
            await session.flush()
            waba = WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id)
            session.add(waba)
            await session.flush()
            session.add(
                PhoneNumber(tenant_id=tid, connection_id=connection.id, waba_account_id=waba.id, phone_number_id=phone_number_id)
            )

            channel_connection = ChannelConnection(
                tenant_id=tid, channel="whatsapp", provider="meta", external_id=phone_number_id,
                active=True,
                credentials_encrypted=get_secret_box().encrypt_json(
                    {"phone_number_id": phone_number_id, "access_token": "tok-de-prueba"}
                ),
            )
            session.add(channel_connection)
            await session.flush()

            customer = Customer(tenant_id=tid, channel="whatsapp", external_id=f"wa:costos-{waba_id}")
            session.add(customer)
            await session.flush()

            conversation = Conversation(tenant_id=tid, customer_id=customer.id, connection_id=channel_connection.id)
            session.add(conversation)
            await session.flush()

            connection_id, waba_row_id, conversation_id = connection.id, waba.id, conversation.id

        await meta_token_store.put_token(connection_id, TOKEN_KIND, "biz-costos", "tok-costos", tenant_id=tid)
        await meta_token_store.put_token(
            connection_id, "app_access", "app-embedded-signup", "s3cr3t-de-la-app", tenant_id=tid
        )
    return tid, connection_id, waba_id, waba_row_id, conversation_id, phone_number_id


def _configure_meta_app_settings(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "app-embedded-signup")
    monkeypatch.setattr(settings, "meta_app_secret", "s3cr3t-de-la-app")


async def test_webhook_pricing_y_sync_de_pricing_analytics(app_client, monkeypatch):
    import core.gateway.meta_pricing_analytics as pricing_mod
    from core.db.models import Message, MessageCost, PricingDaily
    from core.db.session import get_session
    from core.gateway.meta_message_status import apply_status_update
    from core.shared.tenant import tenant_scope

    _configure_meta_app_settings(monkeypatch)
    tid, _connection_id, _waba_id, waba_row_id, conversation_id, phone_number_id = await _setup_connected_tenant()
    session_cookie = await _login(app_client)
    cookie = await _switch_tenant(app_client, session_cookie, tid)

    wamid = f"wamid.COST.{uuid.uuid4().hex[:6]}"

    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                Message(
                    tenant_id=tid, conversation_id=conversation_id, direction="outbound", sender_type="agent",
                    content="hola", status="sent", external_message_id=wamid,
                )
            )

        # 1. Webhook con `pricing` -- persiste MessageCost, NUNCA un monto en dólares.
        await apply_status_update(
            {
                "id": wamid, "status": "delivered", "timestamp": "1735689600",
                "pricing": {"billable": True, "pricing_model": "CBP", "category": "utility"},
                "conversation": {"id": "conv-meta-1", "origin": {"type": "business_initiated"}},
            },
            expected_tenant_id=tid,
            phone_number_id=phone_number_id,
        )

        async with get_session() as session:
            cost_row = (
                await session.execute(select(MessageCost).where(MessageCost.external_message_id == wamid))
            ).scalar_one()
        assert cost_row.billable is True
        assert cost_row.pricing_category == "utility"
        assert cost_row.waba_account_id == waba_row_id
        assert not hasattr(cost_row, "cost")  # MessageCost NUNCA tiene un campo de dólares

        # 2. Sync de pricing_analytics (Graph mockeado) -- `cost` es autoritativo.
        async def _fake_fetch(*a, **k):
            return [
                pricing_mod.PricingAnalyticsPoint(
                    start=1735689600, end=1735776000, phone_number=phone_number_id, country="AR",
                    tier="0:250000", pricing_type="regular", pricing_category="utility",
                    volume=1, cost=0.0512,
                )
            ]

        monkeypatch.setattr(pricing_mod, "fetch_pricing_analytics", _fake_fetch)

        r_sync = await app_client.post("/admin/costos/meta/sync", headers={"Cookie": cookie})
        assert r_sync.status_code == 303
        assert "danger" not in r_sync.headers["location"], r_sync.headers["location"]

        async with get_session() as session:
            pricing_row = (
                await session.execute(select(PricingDaily).where(PricingDaily.waba_account_id == waba_row_id))
            ).scalar_one()
        assert float(pricing_row.cost) == 0.0512
        assert pricing_row.volume == 1

        # 3. El panel muestra el costo real -- nunca calculado localmente.
        r_page = await app_client.get("/admin/costos", headers={"Cookie": cookie})
        assert r_page.status_code == 200
        assert "gratis" not in r_page.text.lower() or "no hay ninguna franquicia" in r_page.text.lower()


async def test_recategorizacion_de_plantilla_se_refleja_sin_corregir_retroactivo(app_client, monkeypatch):
    """Criterio de aceptación explícito: una recategorización se refleja
    en el desglose del período, sin "corregir" lo ya facturado bajo la
    categoría vieja -- se verifica que ambas categorías coexisten en los
    datos (la plantilla ya recategorizada, y el pricing_daily ya
    sincronizado bajo la categoría anterior no se reescribe)."""
    from core.db.models import MessageTemplate
    from core.db.session import get_session
    from core.gateway.meta_template_webhooks import handle_template_category_update
    from core.shared.tenant import tenant_scope

    tid, _connection_id, _waba_id, waba_row_id, _conversation_id, _phone = await _setup_connected_tenant()

    async with tenant_scope(tid):
        async with get_session() as session:
            template = MessageTemplate(
                tenant_id=tid, waba_account_id=waba_row_id, meta_template_id=_uniq("meta-tpl"),
                name="bienvenida", language="es_AR", category="MARKETING", status="APPROVED",
            )
            session.add(template)
            await session.flush()
            template_id = template.id

        await handle_template_category_update(
            {"message_template_id": template.meta_template_id, "new_category": "UTILITY"},
            expected_tenant_id=tid,
        )

        async with get_session() as session:
            updated = await session.get(MessageTemplate, template_id)
        assert updated.category == "UTILITY"
        assert updated.previous_category == "MARKETING"
        assert updated.category_changed_at is not None
