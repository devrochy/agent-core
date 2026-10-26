"""Centro de notificaciones (ADR-0167): emisión, feed por permiso, estado por admin, push
idempotente y retención. Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import types
import uuid
from datetime import UTC, datetime, timedelta

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
BASE = "/api/v1/me/notifications"


def _spec(clave: str, **kw):
    from core.notifications.spec import NotificationSpec

    base = {
        "event_key": "core.escalation",
        "category": "conversaciones",
        "severity": "critical",
        "title": "Nueva escalación",
        "body": "Un cliente pidió atención humana.",
        "dedupe_key": clave,
        "section": "conversations",
        "data": {"type": "core.escalation"},
        "target": {"entity": "conversacion", "id": clave, "section": "conversations"},
    }
    return NotificationSpec(**{**base, **kw})


async def _tenant_con_owner(slug: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    from core.alta_negocio import onboard_tenant
    from core.db.models import TenantUser
    from core.db.session import get_session

    email = f"{slug}@test.local"
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Negocio de prueba de notificaciones.",
        admin_email=email,
        admin_password=PW,
        packs=[],
    )
    async with get_session() as session:
        admin_id = (
            await session.execute(
                select(TenantUser.admin_id).where(TenantUser.tenant_id == tid, TenantUser.role == "owner")
            )
        ).scalar_one()
    return tid, admin_id, email


async def _colaborador(tid: uuid.UUID, *, con_acceso: tuple[str, ...] = ()) -> tuple[uuid.UUID, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('colab')}@test.local"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="colaborador"))
        for seccion in con_acceso:
            session.add(AdminPermission(admin_id=admin.id, tenant_id=tid, section=seccion, access="read"))
        admin_id = admin.id
    return admin_id, email


async def _emitir(tid: uuid.UUID, clave: str, **kw) -> uuid.UUID | None:
    from core.notifications.service import notify_standalone

    return await notify_standalone(tid, _spec(clave, **kw))


async def _agregar_token(tid: uuid.UUID, admin_id: uuid.UUID) -> str:
    from core.db.models import PushToken
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    token = f"ExponentPushToken[{uuid.uuid4().hex}]"
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(PushToken(tenant_id=tid, admin_id=admin_id, token=token))
    return token


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


# ----------------------------------------------------------------------------- emisión


async def test_notify_inserta_emite_al_outbox_y_deduplica(app_client) -> None:
    from core.db.models import DomainEvent, Notification
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, _admin, _email = await _tenant_con_owner(uniq("nt-emit"))
    primero = await _emitir(tid, "esc:1")
    repetido = await _emitir(tid, "esc:1")
    assert primero is not None
    assert repetido is None

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (await session.execute(select(Notification).where(Notification.tenant_id == tid))).scalars().all()
            eventos = (
                await session.execute(
                    select(DomainEvent).where(
                        DomainEvent.tenant_id == tid, DomainEvent.topic == "notification.created"
                    )
                )
            ).scalars().all()
    assert len(filas) == 1 and filas[0].category == "conversaciones"
    assert len(eventos) == 1 and eventos[0].payload["notification_id"] == str(primero)


async def test_notify_con_spec_invalida_no_propaga_ni_revierte_al_llamador(app_client) -> None:
    from core.db.models import Notification, PushToken
    from core.db.session import get_session
    from core.notifications.service import notify
    from core.shared.tenant import tenant_scope

    tid, admin_id, _email = await _tenant_con_owner(uniq("nt-safe"))
    token = f"ExponentPushToken[{uuid.uuid4().hex}]"
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(PushToken(tenant_id=tid, admin_id=admin_id, token=token))
            # El hecho del llamador (el token) debe sobrevivir a una notificación defectuosa.
            resultado = await notify(session, tid, _spec("mala", category="inventada"))
            assert resultado is None
    async with tenant_scope(tid):
        async with get_session() as session:
            tok = (await session.execute(select(PushToken).where(PushToken.token == token))).scalar_one_or_none()
            n = (await session.execute(select(Notification).where(Notification.tenant_id == tid))).scalars().all()
    assert tok is not None
    assert n == []


# ----------------------------------------------------------------------------- API del feed


async def test_feed_lista_cuenta_marca_y_archiva(app_client) -> None:
    tid, _admin, email = await _tenant_con_owner(uniq("nt-api"))
    ids = [await _emitir(tid, f"e:{i}") for i in range(3)]
    h = await api_headers(app_client, tid, email=email, password=PW)

    r = await app_client.get(BASE, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unread_count"] == 3 and len(body["items"]) == 3
    assert body["items"][0]["read"] is False
    assert body["items"][0]["target"]["entity"] == "conversacion"

    r = await app_client.post(f"{BASE}/read", headers=h, json={"ids": [str(ids[0])]})
    assert r.json()["unread_count"] == 2
    r = await app_client.get(f"{BASE}?unread=true", headers=h)
    assert len(r.json()["items"]) == 2

    r = await app_client.delete(f"{BASE}/{ids[1]}", headers=h)
    assert r.status_code == 204
    r = await app_client.get(f"{BASE}/unread-count", headers=h)
    assert r.json()["unread_count"] == 1
    r = await app_client.get(BASE, headers=h)
    assert str(ids[1]) not in {i["id"] for i in r.json()["items"]}

    r = await app_client.post(f"{BASE}/read", headers=h, json={"all": True})
    assert r.json()["unread_count"] == 0
    r = await app_client.get(BASE, headers=h)
    assert all(i["read"] for i in r.json()["items"])


async def test_feed_pagina_con_cursor_y_filtra_por_categoria(app_client) -> None:
    tid, _admin, email = await _tenant_con_owner(uniq("nt-pag"))
    for i in range(5):
        await _emitir(tid, f"p:{i}")
    await _emitir(tid, "venta:1", category="ventas", severity="info", event_key="commerce.lead")
    h = await api_headers(app_client, tid, email=email, password=PW)

    pag1 = (await app_client.get(f"{BASE}?limit=4", headers=h)).json()
    assert len(pag1["items"]) == 4 and pag1["next_cursor"]
    pag2 = (await app_client.get(f"{BASE}?limit=4&cursor={pag1['next_cursor']}", headers=h)).json()
    assert len(pag2["items"]) == 2 and pag2["next_cursor"] is None
    vistos = {i["id"] for i in pag1["items"]} | {i["id"] for i in pag2["items"]}
    assert len(vistos) == 6

    ventas = (await app_client.get(f"{BASE}?category=ventas", headers=h)).json()
    assert [i["category"] for i in ventas["items"]] == ["ventas"]
    r = await app_client.get(f"{BASE}?category=nada", headers=h)
    assert r.status_code == 422


@pytest.mark.aislamiento
async def test_un_negocio_no_ve_las_notificaciones_de_otro(app_client) -> None:
    tid_a, _a, email_a = await _tenant_con_owner(uniq("nt-iso-a"))
    tid_b, _b, email_b = await _tenant_con_owner(uniq("nt-iso-b"))
    nid = await _emitir(tid_a, "secreta:1")
    assert nid is not None

    ha = await api_headers(app_client, tid_a, email=email_a, password=PW)
    hb = await api_headers(app_client, tid_b, email=email_b, password=PW)
    assert (await app_client.get(f"{BASE}/unread-count", headers=ha)).json()["unread_count"] == 1
    assert (await app_client.get(f"{BASE}/unread-count", headers=hb)).json()["unread_count"] == 0
    assert (await app_client.get(BASE, headers=hb)).json()["items"] == []
    # Archivar por id ajeno: 404, no 403 (no se revela que existe).
    assert (await app_client.delete(f"{BASE}/{nid}", headers=hb)).status_code == 404
    # Marcar leída un id ajeno se ignora y no cambia el contador del dueño legítimo.
    await app_client.post(f"{BASE}/read", headers=hb, json={"ids": [str(nid)]})
    assert (await app_client.get(f"{BASE}/unread-count", headers=ha)).json()["unread_count"] == 1


@pytest.mark.aislamiento
async def test_colaborador_ve_solo_las_secciones_con_acceso_explicito(app_client) -> None:
    tid, _admin, _email = await _tenant_con_owner(uniq("nt-perm"))
    _sin, email_sin = await _colaborador(tid)
    _con, email_con = await _colaborador(tid, con_acceso=("conversations",))
    await _emitir(tid, "c:1")  # section conversations
    await _emitir(tid, "s:1", section="settings", category="sistema", severity="warning")
    await _emitir(tid, "n:1", section=None, category="sistema", severity="info")

    h_sin = await api_headers(app_client, tid, email=email_sin, password=PW)
    h_con = await api_headers(app_client, tid, email=email_con, password=PW)
    assert (await app_client.get(BASE, headers=h_sin)).json()["items"] == []
    assert (await app_client.get(f"{BASE}/unread-count", headers=h_sin)).json()["unread_count"] == 0
    con = (await app_client.get(BASE, headers=h_con)).json()
    assert [i["event_key"] for i in con["items"]] == ["core.escalation"]
    assert con["unread_count"] == 1


async def test_master_ve_las_del_negocio_seleccionado(app_client) -> None:
    tid, _admin, _email = await _tenant_con_owner(uniq("nt-master"))
    await _emitir(tid, "m:1")
    h = await api_headers(app_client, tid)  # admin@local (master)
    r = await app_client.get(BASE, headers=h)
    assert r.status_code == 200 and r.json()["unread_count"] == 1


async def test_preferencias_y_silencio(app_client) -> None:
    tid, _admin, email = await _tenant_con_owner(uniq("nt-pref"))
    h = await api_headers(app_client, tid, email=email, password=PW)

    p = (await app_client.get(f"{BASE}/preferences", headers=h)).json()
    assert p["push_off_categories"] == [] and p["muted_until"] is None
    r = await app_client.put(f"{BASE}/preferences", headers=h, json={"push_off_categories": ["ventas", "agenda"]})
    assert r.json()["push_off_categories"] == ["agenda", "ventas"]
    r = await app_client.put(f"{BASE}/preferences", headers=h, json={"push_off_categories": ["x"]})
    assert r.status_code == 422
    r = await app_client.put(f"{BASE}/mute", headers=h, json={"minutes": 60})
    assert r.json()["muted_until"] is not None
    r = await app_client.put(f"{BASE}/mute", headers=h, json={"minutes": 0})
    assert r.json()["muted_until"] is None


# ----------------------------------------------------------------------------- push


async def test_push_se_envia_una_sola_vez_aunque_el_outbox_reintente(app_client, monkeypatch) -> None:
    from core.db.models import Notification
    from core.db.session import get_session
    from core.notifications.service import enviar_push_de_notificacion
    from core.shared.tenant import tenant_scope

    tid, admin_id, _email = await _tenant_con_owner(uniq("nt-push"))
    await _agregar_token(tid, admin_id)
    posts = _push_habilitado(monkeypatch)
    nid = await _emitir(tid, "push:1")

    async with tenant_scope(tid):
        await enviar_push_de_notificacion(nid)
        await enviar_push_de_notificacion(nid)  # reintento del outbox

    assert len(posts) == 1 and len(posts[0]) == 1
    msg = posts[0][0]
    assert msg["channelId"] == "solvershub-conversaciones"
    assert msg["priority"] == "high" and msg["ttl"] > 0
    assert msg["badge"] == 1
    assert msg["data"]["notificationId"] == str(nid) and msg["data"]["type"] == "core.escalation"
    assert msg["data"]["target"]["entity"] == "conversacion"
    async with tenant_scope(tid):
        async with get_session() as session:
            n = (await session.execute(select(Notification).where(Notification.id == nid))).scalar_one()
    assert n.push_sent_at is not None


async def test_push_respeta_permiso_preferencias_y_silencio(app_client, monkeypatch) -> None:
    from core.db.models import NotificationState
    from core.db.session import get_session
    from core.notifications.service import enviar_push_de_notificacion
    from core.shared.tenant import tenant_scope

    tid, owner_id, _email = await _tenant_con_owner(uniq("nt-dest"))
    sin_id, _ = await _colaborador(tid)
    con_id, _ = await _colaborador(tid, con_acceso=("conversations",))
    apagado_id, _ = await _colaborador(tid, con_acceso=("conversations",))
    silenciado_id, _ = await _colaborador(tid, con_acceso=("conversations",))
    tokens = {}
    for nombre, aid in {"owner": owner_id, "sin": sin_id, "con": con_id, "apagado": apagado_id, "silenciado": silenciado_id}.items():
        tokens[nombre] = await _agregar_token(tid, aid)
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(NotificationState(tenant_id=tid, admin_id=apagado_id, push_off_categories=["conversaciones"]))
            session.add(
                NotificationState(
                    tenant_id=tid, admin_id=silenciado_id, muted_until=datetime.now(UTC) + timedelta(hours=1)
                )
            )
    posts = _push_habilitado(monkeypatch)
    nid = await _emitir(tid, "dest:1")
    async with tenant_scope(tid):
        await enviar_push_de_notificacion(nid)

    destinatarios = {m["to"] for lote in posts for m in lote}
    assert destinatarios == {tokens["owner"], tokens["con"]}


async def test_push_se_envia_en_lotes_de_100(app_client, monkeypatch) -> None:
    from core.notifications.push import enviar_push
    from core.shared.tenant import tenant_scope

    tid, admin_id, _email = await _tenant_con_owner(uniq("nt-lote"))
    for _ in range(230):
        await _agregar_token(tid, admin_id)
    posts = _push_habilitado(monkeypatch)
    async with tenant_scope(tid):
        await enviar_push(tid, [admin_id], "T", "B", {"type": "x"})
    assert [len(p) for p in posts] == [100, 100, 30]


async def test_push_deshabilitado_marca_enviada_sin_llamar_a_expo(app_client, monkeypatch) -> None:
    from core.db.models import Notification
    from core.db.session import get_session
    from core.notifications import push
    from core.notifications.service import enviar_push_de_notificacion
    from core.shared.tenant import tenant_scope

    tid, admin_id, _email = await _tenant_con_owner(uniq("nt-off"))
    await _agregar_token(tid, admin_id)
    monkeypatch.setattr(push, "get_settings", lambda: types.SimpleNamespace(expo_push_enabled=False))
    nid = await _emitir(tid, "off:1")
    async with tenant_scope(tid):
        await enviar_push_de_notificacion(nid)
        async with get_session() as session:
            n = (await session.execute(select(Notification).where(Notification.id == nid))).scalar_one()
    assert n.push_sent_at is not None


# ----------------------------------------------------------------------------- retención


async def test_retencion_da_de_baja_las_viejas_leidas_o_no(app_client) -> None:
    from core.db.models import Notification
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tasks.notifications_retention import retener_notificaciones

    tid, _admin, _email = await _tenant_con_owner(uniq("nt-ret"))
    vieja = await _emitir(tid, "vieja")
    nueva = await _emitir(tid, "nueva")
    async with tenant_scope(tid):
        async with get_session() as session:
            n = (await session.execute(select(Notification).where(Notification.id == vieja))).scalar_one()
            n.created_at = datetime.now(UTC) - timedelta(days=181)
    resultado = await retener_notificaciones()
    assert resultado["dadas_de_baja"] >= 1
    async with tenant_scope(tid):
        async with get_session() as session:
            v = (await session.execute(select(Notification).where(Notification.id == vieja))).scalar_one()
            nv = (await session.execute(select(Notification).where(Notification.id == nueva))).scalar_one()
    assert v.deleted_at is not None and nv.deleted_at is None
