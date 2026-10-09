"""Ciclo de vida de tenants: suspensión y purga (ADR-0001, invariante #8).

Genéricos: se crean tenants desechables por test, nunca por cliente. La suspensión
bloquea webhook y consumer; la purga es borrado físico irreversible (frase exacta
"ELIMINAR <slug>") que vacía core + packs + Redis y la fila tenants.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from conftest import _meta_request
from sqlalchemy import func, select, text

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

CATALOG_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "tenants" / "moto" / "catalog.json"
)
INBOUND_STREAM = "agent_core:inbound"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def _onboard(slug: str, whatsapp_phone: str = "") -> tuple[uuid.UUID, str]:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant desechable {slug}",
        slug=slug,
        business_description="Ciclo de vida (test genérico).",
        persona={"tone": "cercano"},
        packs=["pack_commerce"],
        whatsapp={
            "label": slug,
            "phone_number_id": whatsapp_phone,
        }
        if whatsapp_phone
        else None,
    )
    return tid, slug


async def _seed_catalog(tid: uuid.UUID) -> None:
    from cli.commands.tenant import seed_catalog

    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    await seed_catalog(tid, data)


async def _tenant_tables() -> list[str]:
    from core.db.session import get_session

    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    "select table_name from information_schema.columns "
                    "where column_name = 'tenant_id' and table_schema = 'public'"
                )
            )
        ).scalars().all()
    return sorted(rows)


async def _count_rows(table: str, tenant_id: uuid.UUID) -> int:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            return (
                await session.execute(
                    text(f"select count(*) from {table} where tenant_id = :tid"),
                    {"tid": str(tenant_id)},
                )
            ).scalar()


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
                        "from": wa_id,
                        "id": message_id,
                        "timestamp": "1700000000",
                        "type": "text",
                        "text": {"body": body},
                    }],
                },
            }],
        }],
    }


# ------------------------------------------------------------------ suspensión
async def test_suspendido_no_procesa_webhook(app_client) -> None:
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant

    phone = _uniq("555")
    tid, _slug = await _onboard(_uniq("susp-webhook"), whatsapp_phone=phone)
    await deactivate_tenant(str(tid))

    body, headers = _meta_request(
        _meta_payload(phone, "wa-susp", f"wamid.susp.{uuid.uuid4().hex[:6]}", "hola")
    )
    r = await app_client.post(
        "/webhooks/whatsapp",
        content=body,
        headers=headers,
    )
    assert r.json().get("status") == "ignored", "un tenant suspendido no encola mensajes"

    async with tenant_scope(tid):
        async with get_session() as session:
            n = (
                await session.execute(select(func.count()).select_from(Conversation))
            ).scalar()
    assert n == 0, "no debe crearse ninguna conversación para el tenant suspendido"


async def test_suspendido_descarta_eventos_ya_encolados(app_client, recorded) -> None:
    """Mensajes en el stream de un tenant suspendido se ACKean sin procesar."""
    from core.memory.working import get_redis
    from core.orchestrator.consumer import consume_loop
    from core.tenants import deactivate_tenant

    tid, _slug = await _onboard(_uniq("susp-consumer"))
    await deactivate_tenant(str(tid))

    await get_redis().xadd(
        INBOUND_STREAM,
        {
            "tenant_id": str(tid),
            "provider": "meta",
            "channel": "whatsapp",
            "external_message_id": f"wamid.queued.{uuid.uuid4().hex[:6]}",
            "customer_external_id": "wa-queued",
            "content": "mensaje encolado antes de la suspensión",
            "media": "null",
        },
    )
    recorded.clear()

    consumer = asyncio.create_task(consume_loop())
    await asyncio.sleep(2)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    assert recorded == [], "el consumer no debe procesar mensajes de un tenant suspendido"
    # El evento se consumió (ACK): no debe quedar PENDIENTE en el grupo.
    from core.orchestrator.consumer import GROUP

    summary = await get_redis().xpending(INBOUND_STREAM, GROUP)
    assert summary.get("pending", 0) == 0, "el evento del tenant suspendido debe descartarse (ACK), no reintentarse"


# ------------------------------------------------------------------ purga
async def test_suspendido_no_responde_por_debounce(recorded) -> None:
    """Un mensaje con debounce ya programado no genera respuesta si el tenant se suspendió."""
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.orchestrator.debounce import poll_debounce_once, schedule_debounce
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant

    tid, slug = await _onboard(_uniq("susp-debounce"))
    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="whatsapp", external_id="wa-deb")
            session.add(customer)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=customer.id, channel="whatsapp")
            session.add(conv)
            await session.commit()
            conv_id = conv.id

    # debounce programado con delay corto (como si el mensaje llegara pre-suspensión)
    await schedule_debounce(conv_id, 500, {"latest_message": "hola", "tenant_id": str(tid)})
    await deactivate_tenant(slug)
    recorded.clear()

    await poll_debounce_once()

    assert recorded == [], "un tenant suspendido no debe generar turnos por el debounce"
    from core.db.models import Message
    async with tenant_scope(tid):
        async with get_session() as session:
            outbound = (
                await session.execute(
                    select(func.count()).select_from(Message).where(Message.direction == "outbound")
                )
            ).scalar()
    assert outbound == 0, "no debe enviarse ninguna respuesta del tenant suspendido"


async def test_purge_frase_incorrecta_no_borra() -> None:
    from core.db.models import Tenant
    from core.db.session import get_session
    from core.tenants import purge_tenant

    tid, _slug = await _onboard(_uniq("purge-nofrase"))
    await _seed_catalog(tid)

    with pytest.raises(ValueError):
        await purge_tenant(str(tid), "ELIMINAR OTRACOSA")

    async with get_session() as session:
        still = await session.get(Tenant, tid)
    assert still is not None, "una frase incorrecta no borra nada"


async def test_purge_borra_core_packs_redis_y_tenant() -> None:
    from core.db.models import Admin, Conversation, Customer, Message, Tenant
    from core.db.session import get_session
    from core.memory.working import append_turn, get_redis
    from core.shared.tenant import admin_tenants, tenant_scope
    from core.tenants import purge_tenant

    tid, slug = await _onboard(_uniq("purge-todo"))
    await _seed_catalog(tid)

    # cliente + conversación + mensaje + working memory en Redis
    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(tenant_id=tid, channel="whatsapp", external_id="wa-purge")
            session.add(customer)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=customer.id, channel="whatsapp", status="open")
            session.add(conv)
            await session.flush()
            session.add(
                Message(
                    tenant_id=tid, conversation_id=conv.id, direction="inbound",
                    sender_type="customer", content="hola",
                )
            )
            await session.commit()
            conv_id = conv.id
    await append_turn(conv_id, "user", "hola")

    tables = await _tenant_tables()
    assert tables, "debe haber tablas con tenant_id"
    assert sum([await _count_rows(t, tid) for t in tables]) > 0, "el tenant debe tener datos"

    await purge_tenant(str(tid), f"ELIMINAR {slug}")

    # cero filas en TODAS las tablas (core + packs)
    for t in tables:
        assert await _count_rows(t, tid) == 0, f"{t} debería estar vacío tras el purge"

    # la fila tenants ya no existe
    async with get_session() as session:
        assert await session.get(Tenant, tid) is None, "la fila tenants debe desaparecer"

    # Redis: la working memory de la conversación se borró
    assert await get_redis().exists(f"wm:{conv_id}") == 0, "working memory del tenant purgada"

    # el admin pierde acceso al tenant
    async with get_session() as session:
        admin = (
            await session.execute(select(Admin).where(Admin.email == "admin@local"))
        ).scalar_one_or_none()
    if admin is not None:
        ids = {str(t[0]) for t in await admin_tenants(admin.id)}
        assert str(tid) not in ids, "tras el purge, el admin no puede ver el tenant"


# ------------------------------------------------------------------ panel
async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def test_panel_desactivar_reactivar_y_purge(app_client) -> None:
    from core.db.models import Tenant
    from core.db.session import get_session

    tid, slug = await _onboard(_uniq("panel-lifecycle"))
    cookie = await _login(app_client)
    headers = {"Cookie": cookie}

    r = await app_client.post(f"/admin/tenants/{tid}/deactivate", headers=headers)
    assert r.status_code == 303
    async with get_session() as session:
        assert (await session.get(Tenant, tid)).status == "suspended"

    r = await app_client.post(f"/admin/tenants/{tid}/activate", headers=headers)
    assert r.status_code == 303
    async with get_session() as session:
        assert (await session.get(Tenant, tid)).status == "active"

    # frase incorrecta → no borra
    r = await app_client.post(
        f"/admin/tenants/{tid}/purge", headers=headers, data={"confirm_phrase": "ELIMINAR OTRACOSA"}
    )
    assert r.status_code == 303 and "danger" in r.headers["location"]
    async with get_session() as session:
        assert await session.get(Tenant, tid) is not None

    # frase correcta → borra
    r = await app_client.post(
        f"/admin/tenants/{tid}/purge",
        headers=headers,
        data={"confirm_phrase": f"ELIMINAR {slug}"},
    )
    assert r.status_code == 303 and "success" in r.headers["location"]
    async with get_session() as session:
        assert await session.get(Tenant, tid) is None