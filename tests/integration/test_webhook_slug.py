"""Webhook por tenant: `/webhooks/whatsapp/<slug>` (cada app su propio webhook).

Genérico: se crea un tenant desechable, se registra su webhook (handshake GET) y
se verifica que los mensajes POST se procesan con el tenant resuelto por slug.
"""

from __future__ import annotations

import asyncio
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


def _meta_payload(phone_id: str, wa_id: str, message_id: str, body: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "e1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": phone_id},
                    "contacts": [{"wa_id": wa_id}],
                    "messages": [{
                        "from": wa_id, "id": message_id, "timestamp": "1700000000",
                        "type": "text", "text": {"body": body},
                    }],
                },
            }],
        }],
    }


async def _onboard(slug: str, phone: str, verify_token: str) -> uuid.UUID:
    from core.onboarding import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Webhook {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        whatsapp={
            "label": slug,
            "phone_number_id": phone,
            "app_secret": "",
            "access_token": "",
            "verify_token": verify_token,
        },
    )
    return tid


async def test_handshake_por_slug(app_client, recorded) -> None:
    """GET /webhooks/whatsapp/<slug> devuelve el challenge si el verify_token coincide."""
    slug = f"wh-{uuid.uuid4().hex[:6]}"
    token = f"miToken{slug}"
    await _onboard(slug, f"5551{uuid.uuid4().hex[:6]}", token)

    # token correcto → 200 + challenge
    r = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "CHAL123"},
    )
    assert r.status_code == 200 and r.text == "CHAL123"

    # token incorrecto → 403
    r2 = await app_client.get(
        "/webhooks/whatsapp/" + slug,
        params={"hub.mode": "subscribe", "hub.verify_token": "malo", "hub.challenge": "CHAL123"},
    )
    assert r2.status_code == 403

    # slug inexistente → 403
    r3 = await app_client.get(
        "/webhooks/whatsapp/no-existe",
        params={"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "CHAL123"},
    )
    assert r3.status_code == 403


async def test_post_por_slug_procesa(app_client, recorded) -> None:
    """POST /webhooks/whatsapp/<slug> encola y el consumer responde con el tenant correcto."""
    from sqlalchemy import select as sa_select

    from core.orchestrator.consumer import consume_loop

    slug = f"wh-{uuid.uuid4().hex[:6]}"
    tid = await _onboard(slug, f"5552{uuid.uuid4().hex[:6]}", "tok")
    # modo inmediato (sin debounce de 15s) para que el turno se dispare ya.
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            row = (
                await session.execute(sa_select(AppSettings).where(AppSettings.tenant_id == tid))
            ).scalar_one_or_none()
            behavior = dict(row.behavior_config or {})
            behavior["debounce_mode"] = "inmediato"
            row.behavior_config = behavior
            await session.commit()

    recorded.clear()

    r = await app_client.post(
        f"/webhooks/whatsapp/{slug}",
        json=_meta_payload("cualquiera", "wa-slug", f"wamid.slug.{uuid.uuid4().hex[:6]}", "hola slug"),
    )
    assert r.json().get("status") == "ok" and r.json().get("enqueued") == 1

    consumer = asyncio.create_task(consume_loop())
    for _ in range(40):
        if recorded:
            break
        await asyncio.sleep(0.2)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    assert recorded, "el webhook por slug debe procesar el mensaje del tenant"
    # La conversación queda asociada al tenant (no hay fuga).
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            from core.db.models import Conversation

            n = (
                await session.execute(select(__import__("sqlalchemy").func.count()).select_from(Conversation))
            ).scalar()
    assert n == 1, "la conversación se crea bajo el tenant del slug"


async def test_tone_invalido_rechazado() -> None:
    """El onboarding NO persiste un spec fuera del contrato (evita el 500 en /admin/spec)."""
    from core.onboarding import onboard_tenant

    with pytest.raises(ValueError):
        await onboard_tenant(
            name="Tone malo",
            slug=f"tone-{uuid.uuid4().hex[:6]}",
            persona={"tone": "calido"},
        )