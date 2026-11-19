"""Envío de push (ADR-0099): transporte Expo, bajas automáticas y disparadores.

El transporte HTTP se mockea; se prueba la resolución de tokens con RLS, el
lote, la baja de `DeviceNotRegistered`, el disparo tras `escalate()` y el gate
del sync de campo (solo gestores). Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import types
import uuid

import pytest
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "password123"


def _settings_push_enabled(monkeypatch) -> None:
    from core.notifications import push

    monkeypatch.setattr(
        push,
        "get_settings",
        lambda: types.SimpleNamespace(
            expo_push_enabled=True,
            expo_push_url="https://expo.test/send",
            expo_push_access_token="",
        ),
    )


async def _tenant_con_owner(email: str, slug: str) -> tuple[uuid.UUID, uuid.UUID]:
    from core.alta_negocio import onboard_tenant
    from core.db.models import TenantUser
    from core.db.session import get_session

    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Negocio de prueba de push.",
        admin_email=email,
        admin_password=PW,
        packs=[],
    )
    async with get_session() as session:
        admin_id = (
            await session.execute(
                select(TenantUser.admin_id).where(
                    TenantUser.tenant_id == tid, TenantUser.role == "owner"
                )
            )
        ).scalar_one()
    return tid, admin_id


async def _agregar_token(tid: uuid.UUID, admin_id: uuid.UUID, token: str) -> None:
    from core.db.models import PushToken
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(PushToken(tenant_id=tid, admin_id=admin_id, token=token))
            await session.commit()


async def test_enviar_push_envia_lote_a_tokens_activos(app_client, monkeypatch) -> None:
    from core.notifications.push import enviar_push
    from core.shared.tenant import tenant_scope

    slug = uniq("push-e")
    tid, admin_id = await _tenant_con_owner(f"{slug}@test.local", slug)
    expo_token = f"ExponentPushToken[test-{uuid.uuid4().hex}]"
    await _agregar_token(tid, admin_id, expo_token)

    capturados: list[list[dict]] = []

    async def fake_post(payloads: list[dict]) -> list[dict]:
        capturados.append(payloads)
        return [{"status": "ok", "id": f"ticket-{i}"} for i in range(len(payloads))]

    monkeypatch.setattr("core.notifications.push._post_expo", fake_post)
    _settings_push_enabled(monkeypatch)

    async with tenant_scope(tid):
        await enviar_push(
            tid,
            [admin_id],
            "Título",
            "Cuerpo",
            {"type": "core.escalation"},
        )

    assert len(capturados) == 1
    msg = capturados[0][0]
    assert msg["to"] == expo_token
    assert msg["title"] == "Título" and msg["body"] == "Cuerpo"
    assert msg["data"]["type"] == "core.escalation"
    assert msg["data"]["tenantId"] == str(tid)
    # Canal que crea la app (CANAL_DEFAULT en solvershub-apk); sin él Android usa el de
    # respaldo de FCM, sin la importancia ni el sonido configurados.
    assert msg["channelId"] == "solvershub-general"


async def test_device_not_registered_da_de_baja_el_token(app_client, monkeypatch) -> None:
    from core.db.models import PushToken
    from core.db.session import get_session
    from core.notifications.push import enviar_push
    from core.shared.tenant import tenant_scope

    slug = uniq("push-baja")
    tid, admin_id = await _tenant_con_owner(f"{slug}@test.local", slug)
    expo_token = f"ExponentPushToken[test-{uuid.uuid4().hex}]"
    await _agregar_token(tid, admin_id, expo_token)

    async def fake_post(payloads: list[dict]) -> list[dict]:
        return [
            {"status": "error", "details": {"error": "DeviceNotRegistered"}}
            for _ in payloads
        ]

    monkeypatch.setattr("core.notifications.push._post_expo", fake_post)
    _settings_push_enabled(monkeypatch)

    async with tenant_scope(tid):
        await enviar_push(tid, [admin_id], "T", "B", {"type": "x"})

    async with tenant_scope(tid):
        async with get_session() as session:
            activos = (
                await session.execute(
                    select(PushToken).where(
                        PushToken.tenant_id == tid, PushToken.deleted_at.is_(None)
                    )
                )
            ).scalars().all()
    assert activos == []


async def test_escalate_dispara_push_a_duenos(app_client, monkeypatch) -> None:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.orchestrator.escalation import escalate
    from core.shared.tenant import tenant_scope

    slug = uniq("push-esc")
    tid, _admin_id = await _tenant_con_owner(f"{slug}@test.local", slug)

    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(
                tenant_id=tid, channel="whatsapp", external_id=f"wa-{uuid.uuid4().hex[:8]}"
            )
            session.add(customer)
            await session.flush()
            conv = Conversation(
                tenant_id=tid,
                customer_id=customer.id,
                channel="whatsapp",
                status="open",
            )
            session.add(conv)
            await session.flush()
            conv_id = conv.id

    from _api_helpers import capturar_avisos

    avisos = capturar_avisos(monkeypatch, lambda t, spec: (str(t), spec.category, spec.section, spec.target))

    async with tenant_scope(tid):
        await escalate(conv_id, "keyword", "pidió humano")

    assert len(avisos) == 1
    tenant, categoria, _seccion, destino = avisos[0]
    assert tenant == str(tid)
    assert categoria == "conversaciones"
    assert destino["entity"] == "conversacion" and destino["id"] == str(conv_id)


async def test_sync_campo_solo_avisa_si_el_autor_es_gestor(monkeypatch) -> None:
    from _api_helpers import capturar_avisos
    from pack_cobranza.api_gestor import _notificar_actividad_campo

    avisos = capturar_avisos(monkeypatch, lambda t, spec: (str(t), spec.title, spec.data))

    tid = uuid.uuid4()
    await _notificar_actividad_campo(tid, {"rol": "gestor"}, ["pago", "pago", "gasto"])
    assert len(avisos) == 1
    tenant_id, titulo, data = avisos[0]
    assert tenant_id == str(tid)
    assert titulo == "Actividad de campo"
    assert data["tipos"] == ["gasto", "pago"]

    await _notificar_actividad_campo(tid, {"rol": "propietario"}, ["pago"])
    await _notificar_actividad_campo(tid, {"rol": "gestor"}, [])
    assert len(avisos) == 1
