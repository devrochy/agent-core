"""Avisos de handoff (ADR-0167): una persona toma o cierra la atención humana de una
conversación escalada. El equipo con acceso a Escalaciones se entera; quien hizo la acción no
recibe el push y el aviso nace leído para ella. Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import types
import uuid

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "password123"
URL = "/api/v1/admin/conversations"


async def _negocio() -> dict:
    from core.admin.auth import hash_password
    from core.alta_negocio import onboard_tenant
    from core.db.models import (
        Admin,
        AdminPermission,
        Conversation,
        Customer,
        Escalation,
        PushToken,
        TenantUser,
    )
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("handoff")
    email_dueno = f"{slug}@test.local"
    tid, _ = await onboard_tenant(
        name=f"Handoff {slug}",
        slug=slug,
        business_description="Negocio de prueba de handoff.",
        admin_email=email_dueno,
        admin_password=PW,
        packs=[],
    )
    email_colab = f"{uniq('colab')}@test.local"
    async with get_session() as session:
        dueno_id = (
            await session.execute(
                select(TenantUser.admin_id).where(TenantUser.tenant_id == tid, TenantUser.role == "owner")
            )
        ).scalar_one()
        colab = Admin(email=email_colab, password_hash=hash_password(PW), role="colaborador")
        session.add(colab)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=colab.id, role="colaborador"))
        session.add(AdminPermission(admin_id=colab.id, tenant_id=tid, section="escalations", access="read"))
        colab_id = colab.id
    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name="Ana")
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id, status="escalated", bot_paused=True)
            session.add(conv)
            await session.flush()
            session.add(Escalation(tenant_id=tid, conversation_id=conv.id, reason="tool:queja", summary="x"))
            session.add(PushToken(tenant_id=tid, admin_id=dueno_id, token=f"ExponentPushToken[{uuid.uuid4().hex}]"))
            session.add(PushToken(tenant_id=tid, admin_id=colab_id, token=f"ExponentPushToken[{uuid.uuid4().hex}]"))
            conv_id = conv.id
    return {
        "tid": tid,
        "conv": conv_id,
        "dueno": dueno_id,
        "colab": colab_id,
        "email_dueno": email_dueno,
        "email_colab": email_colab,
    }


async def _avisos(tid: uuid.UUID, clave: str):
    from core.db.models import Notification
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (
                await session.execute(select(Notification).where(Notification.event_key == clave))
            ).scalars().all()


def _push_habilitado(monkeypatch) -> list[list[dict]]:
    from core.notifications import push

    monkeypatch.setattr(
        push,
        "get_settings",
        lambda: types.SimpleNamespace(
            expo_push_enabled=True, expo_push_url="https://expo.test/send", expo_push_access_token=""
        ),
    )
    posts: list[list[dict]] = []

    async def fake_post(payloads: list[dict]) -> list[dict]:
        posts.append(payloads)
        return [{"status": "ok", "id": f"t{i}"} for i in range(len(payloads))]

    monkeypatch.setattr(push, "_post_expo", fake_post)
    return posts


async def _destinatarios(tid: uuid.UUID, notificacion) -> set[uuid.UUID]:
    from core.db.session import get_session
    from core.notifications.service import destinatarios_de_push
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            elegidos, _ = await destinatarios_de_push(session, notificacion)
    return set(elegidos)


async def test_tomar_avisa_al_equipo_pero_no_a_quien_la_toma(app_client) -> None:
    n = await _negocio()
    h = await api_headers(app_client, n["tid"], email=n["email_dueno"], password=PW)

    r = await app_client.post(f"{URL}/{n['conv']}/take", headers=h)
    assert r.status_code == 200, r.text
    # Idempotente: tomarla otra vez no repite el aviso.
    assert (await app_client.post(f"{URL}/{n['conv']}/take", headers=h)).status_code == 200

    avisos = await _avisos(n["tid"], "core.handoff_assigned")
    assert len(avisos) == 1
    aviso = avisos[0]
    assert aviso.category == "conversaciones" and aviso.severity == "info"
    assert n["email_dueno"] in aviso.body
    assert aviso.target == {"entity": "conversacion", "id": str(n["conv"]), "section": "escalations"}

    # El colaborador con acceso a Escalaciones recibe el push; quien la tomó, no.
    assert await _destinatarios(n["tid"], aviso) == {n["colab"]}

    # Para quien la tomó el aviso nace leído; para el colaborador queda sin leer.
    hc = await api_headers(app_client, n["tid"], email=n["email_colab"], password=PW)
    assert (await app_client.get("/api/v1/me/notifications/unread-count", headers=h)).json()["unread_count"] == 0
    assert (await app_client.get("/api/v1/me/notifications/unread-count", headers=hc)).json()["unread_count"] == 1


async def test_devolver_al_agente_avisa_una_sola_vez(app_client, monkeypatch) -> None:
    from core.notifications.service import enviar_push_de_notificacion
    from core.shared.tenant import tenant_scope

    n = await _negocio()
    posts = _push_habilitado(monkeypatch)
    h = await api_headers(app_client, n["tid"], email=n["email_dueno"], password=PW)

    assert (await app_client.post(f"{URL}/{n['conv']}/take", headers=h)).status_code == 200
    assert (await app_client.post(f"{URL}/{n['conv']}/return-to-bot", headers=h)).status_code == 204
    # Sin escalación abierta no hay nada que avisar.
    assert (await app_client.post(f"{URL}/{n['conv']}/return-to-bot", headers=h)).status_code == 204

    avisos = await _avisos(n["tid"], "core.handoff_resolved")
    assert len(avisos) == 1
    aviso = avisos[0]
    assert aviso.severity == "success" and "devolvió" in aviso.body

    async with tenant_scope(n["tid"]):
        await enviar_push_de_notificacion(aviso.id)
    assert len(posts) == 1 and len(posts[0]) == 1  # solo el colaborador, no quien la devolvió
    assert posts[0][0]["data"]["type"] == "core.handoff_resolved"


async def test_resolver_avisa_con_su_propio_texto(app_client) -> None:
    n = await _negocio()
    h = await api_headers(app_client, n["tid"], email=n["email_dueno"], password=PW)

    assert (await app_client.post(f"{URL}/{n['conv']}/resolve", headers=h)).status_code == 204

    avisos = await _avisos(n["tid"], "core.handoff_resolved")
    assert len(avisos) == 1 and "cerró la atención humana" in avisos[0].body
