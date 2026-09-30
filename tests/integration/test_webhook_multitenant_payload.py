"""Un solo POST al webhook GENÉRICO con entries de dos tenants distintos
(pasa cuando comparten la misma App de Meta) -- cada entry se rutea, procesa
y audita bajo SU propio tenant, nunca el del primero."""

from __future__ import annotations

import hashlib
import hmac
import json
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


async def _onboard_with_phone(slug: str, phone: str) -> uuid.UUID:
    from core.onboarding import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant multitenant-payload {slug}",
        slug=slug,
        business_description="Test de payload multi-tenant.",
        persona={"tone": "cercano"},
        whatsapp={"label": slug, "phone_number_id": phone},
    )
    return tid


async def _post_signed(app_client, payload: dict):
    """Firma el body con el mismo secreto que el endpoint genérico vaya a usar
    (`whatsapp_app_secret` o `meta_app_secret`) -- si ninguno está seteado en
    este entorno, el endpoint no exige firma, igual que hoy."""
    settings = get_settings()
    app_secret = settings.whatsapp_app_secret or settings.meta_app_secret
    body = json.dumps(payload).encode()
    headers = {}
    if app_secret:
        signature = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={signature}"
    return await app_client.post(
        "/webhooks/whatsapp", content=body, headers={**headers, "Content-Type": "application/json"}
    )


def _entry(entry_id: str, phone_id: str, wa_id: str, message_id: str, body: str) -> dict:
    return {
        "id": entry_id,
        "changes": [
            {
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": phone_id},
                    "contacts": [{"wa_id": wa_id, "profile": {"name": "Cliente"}}],
                    "messages": [{"from": wa_id, "id": message_id, "type": "text", "text": {"body": body}}],
                },
            }
        ],
    }


async def test_payload_dos_tenants_2_filas_2_conversaciones(app_client):
    import asyncio

    from core.db.models import Conversation, WebhookEvent
    from core.db.session import get_session
    from core.orchestrator.consumer import consume_loop
    from core.shared.tenant import tenant_scope

    phone_a, phone_b = _uniq("phoneA"), _uniq("phoneB")
    tenant_a = await _onboard_with_phone(_uniq("multi-a"), phone_a)
    tenant_b = await _onboard_with_phone(_uniq("multi-b"), phone_b)

    msg_id_a, msg_id_b = f"wamid.{_uniq('a')}", f"wamid.{_uniq('b')}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            _entry("waba-a", phone_a, "wa-a", msg_id_a, "hola desde A"),
            _entry("waba-b", phone_b, "wa-b", msg_id_b, "hola desde B"),
        ],
    }

    r = await _post_signed(app_client, payload)
    data = r.json()
    assert data["status"] == "ok"
    assert data["enqueued"] == 2

    # consumir el stream para que el orchestrator cree las conversaciones.
    consumer = asyncio.create_task(consume_loop())
    for _ in range(40):
        async with tenant_scope(tenant_a):
            async with get_session() as session:
                done_a = (
                    await session.execute(select(Conversation).where(Conversation.tenant_id == tenant_a))
                ).scalars().all()
        async with tenant_scope(tenant_b):
            async with get_session() as session:
                done_b = (
                    await session.execute(select(Conversation).where(Conversation.tenant_id == tenant_b))
                ).scalars().all()
        if done_a and done_b:
            break
        await asyncio.sleep(0.2)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    async with tenant_scope(tenant_a):
        async with get_session() as session:
            n_a = (
                await session.execute(select(Conversation).where(Conversation.tenant_id == tenant_a))
            ).scalars().all()
            assert len(n_a) == 1

    async with tenant_scope(tenant_b):
        async with get_session() as session:
            n_b = (
                await session.execute(select(Conversation).where(Conversation.tenant_id == tenant_b))
            ).scalars().all()
            assert len(n_b) == 1

    # webhook_events es tabla de sistema, sin RLS (ADR-0028) -- se consulta directo.
    async with get_session() as session:
        row_a = (
            await session.execute(select(WebhookEvent).where(WebhookEvent.external_id == msg_id_a))
        ).scalar_one()
        row_b = (
            await session.execute(select(WebhookEvent).where(WebhookEvent.external_id == msg_id_b))
        ).scalar_one()
    assert row_a.tenant_id == tenant_a
    assert row_b.tenant_id == tenant_b
    assert row_a.process_error is None and row_b.process_error is None


async def test_reenviar_mismo_payload_no_duplica(app_client):
    from core.db.models import WebhookEvent
    from core.db.session import get_session

    phone = _uniq("phoneC")
    await _onboard_with_phone(_uniq("multi-c"), phone)
    msg_id = f"wamid.{_uniq('c')}"
    payload = {"object": "whatsapp_business_account", "entry": [_entry("waba-c", phone, "wa-c", msg_id, "hola")]}

    r1 = await _post_signed(app_client, payload)
    r2 = await _post_signed(app_client, payload)
    assert r1.json()["enqueued"] == 1
    assert r2.json()["duplicates"] == 1  # dedupe de Capa 1 (Redis), sigue funcionando

    async with get_session() as session:
        rows = (
            await session.execute(select(WebhookEvent).where(WebhookEvent.external_id == msg_id))
        ).scalars().all()
    assert len(rows) == 1, "ON CONFLICT DO NOTHING: una sola fila pese a 2 POSTs idénticos"


async def test_evento_sin_tenant_resuelto_no_se_descarta(app_client, monkeypatch):
    from core.db.models import WebhookEvent
    from core.db.session import get_session

    # El fallback a default_tenant() solo aplica en development (bootstrap) --
    # acá se fuerza production para probar el camino real "no se pudo
    # resolver ningún tenant", que es el que importa auditar.
    monkeypatch.setattr(get_settings(), "app_env", "production")

    phone_desconocido = _uniq("no-registrado")
    msg_id = f"wamid.{_uniq('unresolved')}"
    payload = {
        "object": "whatsapp_business_account",
        "entry": [_entry("waba-desconocida", phone_desconocido, "wa-x", msg_id, "hola")],
    }

    r = await _post_signed(app_client, payload)
    assert r.json()["status"] == "ignored"

    async with get_session() as session:
        row = (
            await session.execute(select(WebhookEvent).where(WebhookEvent.external_id == msg_id))
        ).scalar_one()
    assert row.tenant_id is None
    assert row.process_error == "unresolved_tenant"


async def test_webhook_events_insert_falla_devuelve_500(app_client, monkeypatch):
    import core.gateway.webhooks as webhooks_mod

    async def _fails(*a, **k):
        return False  # mismo contrato que _log_webhook_event si el insert real falla

    monkeypatch.setattr(webhooks_mod, "_log_webhook_event", _fails)

    phone = _uniq("phoneFail")
    await _onboard_with_phone(_uniq("multi-fail"), phone)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [_entry("waba-fail", phone, "wa-fail", f"wamid.{_uniq('fail')}", "hola")],
    }
    r = await _post_signed(app_client, payload)
    assert r.status_code == 500
