"""Criterios de salida del Sprint 1 (ROADMAP) contra el stack real.

1. 5 webhooks idénticos en 3 s → exactamente 1 turno.
2. 3 mensajes distintos del mismo contacto en 2 s → 1 turno agrupado.
3. Un tenant encolando muchos mensajes no impide que otro tenant sea atendido.
4. Una query sin tenant_id no devuelve filas de otro tenant (RLS real).
5. `recover_orphaned_conversations` siembra el tenant_id (bug encontrado por
   `auditor-multitenant`, 2026-09-12, sobre el fix de debounce).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from conftest import PHONE, TEST_ALPHA_ID, FakeProvider, _meta_payload
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID


async def _set_behavior(tenant_id: uuid.UUID, debounce_mode: str) -> None:
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = (
                await session.execute(select(AppSettings).where(AppSettings.tenant_id == tenant_id))
            ).scalar_one_or_none()
            if row is None:
                row = AppSettings(tenant_id=tenant_id, assistant_name="it")
                session.add(row)
            behavior = dict(row.behavior_config or {})
            behavior["debounce_mode"] = debounce_mode
            row.behavior_config = behavior


async def _run_background(seconds: float) -> None:
    from core.orchestrator.consumer import consume_loop
    from core.orchestrator.debounce import poll_debounce_loop

    consumer = asyncio.create_task(consume_loop())
    poller = asyncio.create_task(poll_debounce_loop())
    await asyncio.sleep(seconds)
    consumer.cancel()
    poller.cancel()
    for task in (consumer, poller):
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _count(table, **filters) -> int:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T1):
        async with get_session() as session:
            stmt = select(func.count()).select_from(table)
            for col, val in filters.items():
                stmt = stmt.where(getattr(table, col) == val)
            return (await session.execute(stmt)).scalar() or 0


# ---------------------------------------------------------------- 1. dedupe
async def test_cinco_webhooks_identicos_un_turno(app_client, tenant_setup, recorded):
    t1, _ = tenant_setup
    await _set_behavior(t1, "inmediato")
    recorded.clear()

    msg_id = f"wamid.DEDUPE.{uuid.uuid4().hex[:8]}"
    results = []
    for _ in range(5):
        resp = await app_client.post(
            "/webhooks/whatsapp", json=_meta_payload(PHONE, "wa-555", msg_id, "hola")
        )
        results.append(resp.json())
    assert results[0]["enqueued"] == 1
    assert sum(r["duplicates"] for r in results) == 4

    await _run_background(3)

    claims = await _count(__import__("core.db.models", fromlist=["ProcessedEvent"]).ProcessedEvent, external_message_id=msg_id)
    assert claims == 1, f"claims esperados 1, hay {claims}"
    assert len(recorded) == 1, f"turnos esperados 1, hay {len(recorded)}"


# ---------------------------------------------------------------- 2. debounce
async def test_tres_mensajes_en_2s_un_turno(app_client, tenant_setup, recorded):
    t1, _ = tenant_setup
    await _set_behavior(t1, "corto")
    recorded.clear()

    for i in range(3):
        await app_client.post(
            "/webhooks/whatsapp",
            json=_meta_payload(PHONE, "wa-777", f"wamid.BURST.{i}.{uuid.uuid4().hex[:6]}", f"msg {i}"),
        )

    await _run_background(6)

    assert len(recorded) == 1, f"turnos agrupados esperados 1, hay {len(recorded)}"
    # Incidente real (tenant yopoldo, 2026-09-12): agrupar en UN turno no
    # basta si `schedule_debounce` reemplaza el payload en vez de acumularlo
    # -- el turno único terminaba viendo SOLO el último mensaje ("msg 2"), y
    # los dos anteriores (ej. la dirección del pedido) se perdían para
    # siempre aunque quedaran persistidos en `messages`. Este assert es el
    # que hubiera atrapado ese bug (el de arriba, `len(recorded) == 1`,
    # seguía pasando con el bug presente).
    for i in range(3):
        assert f"msg {i}" in recorded[0], f"mensaje 'msg {i}' perdido del turno agrupado: {recorded[0]!r}"


async def test_schedule_debounce_concurrente_no_pierde_mensajes(tenant_setup):
    """Hallazgo de `auditor-multitenant` (2026-09-12) sobre el fix de arriba:
    `consumer.py` crea una task de asyncio por entrada del batch (`count=10`
    en `xreadgroup`) -- dos mensajes del MISMO `conversation_id` pueden
    procesarse concurrentemente. Un GET+merge+SET no atómico pierde uno de
    los dos bajo esa concurrencia (la misma clase de bug que el fix de
    arriba corrige para el caso secuencial). `schedule_debounce` acumula
    con RPUSH (atómico por comando) precisamente para que esto no pase.
    """
    from core.memory.working import get_redis
    from core.orchestrator.debounce import (
        MESSAGES_KEY,
        PAYLOAD_KEY,
        PENDING_KEY,
        schedule_debounce,
    )
    from core.shared.tenant import tenant_scope

    t1, _ = tenant_setup
    conversation_id = uuid.uuid4()
    r = get_redis()
    try:
        async with tenant_scope(t1):
            await asyncio.gather(
                schedule_debounce(conversation_id, 200, {"latest_message": "concurrente A"}),
                schedule_debounce(conversation_id, 200, {"latest_message": "concurrente B"}),
            )

        messages = await r.lrange(MESSAGES_KEY.format(conversation_id=conversation_id), 0, -1)
        assert set(messages) == {"concurrente A", "concurrente B"}, (
            f"se perdió un mensaje bajo llamadas concurrentes: {messages}"
        )
    finally:
        await r.delete(
            MESSAGES_KEY.format(conversation_id=conversation_id),
            PAYLOAD_KEY.format(conversation_id=conversation_id),
        )
        await r.zrem(PENDING_KEY, str(conversation_id))


# ---------------------------------------------------------------- 3. fairness
async def test_tenant_ruidoso_no_bloquea_a_otro(app_client, tenant_setup, recorded, monkeypatch):
    t1, t2 = tenant_setup
    await _set_behavior(t1, "inmediato")
    await _set_behavior(t2, "inmediato")

    # proveedor lento (0.05s por turno) para que el cupo por tenant importe
    recorded.clear()
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: FakeProvider(recorded, delay_s=0.05))

    # tenant A (ruidoso): 20 conversaciones distintas
    for i in range(20):
        await app_client.post(
            "/webhooks/whatsapp",
            json=_meta_payload(PHONE, f"wa-A{i}", f"wamid.A{i}.{uuid.uuid4().hex[:6]}", f"A-{i}"),
        )
    # tenant B: 1 mensaje
    await app_client.post(
        "/webhooks/whatsapp",
        json=_meta_payload("999", "wa-B1", f"wamid.B1.{uuid.uuid4().hex[:6]}", "B-1"),
    )

    async def _b_served() -> bool:
        for _ in range(50):  # hasta 10 s
            if any("B-1" in r for r in recorded):
                return True
            await asyncio.sleep(0.2)
        return False

    from core.orchestrator.consumer import consume_loop

    consumer = asyncio.create_task(consume_loop())
    try:
        assert await _b_served(), "el tenant B nunca fue atendido"
    finally:
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass

    # B fue atendido antes de que A agotara sus 20 turnos
    a_served = sum(1 for r in recorded if r.startswith("A-"))
    assert a_served < 20, f"A drenó todo antes de servir a B ({a_served}/20)"


# ---------------------------------------------------------------- 4. RLS real
async def test_query_sin_tenant_no_ve_otro_tenant(app_client, tenant_setup):
    t1, t2 = tenant_setup
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    # limpia SOLO sus propios clientes de prueba (rls:), sin tocar el resto:
    # usage_ledger es append-only y otras pruebas dejan filas que bloquean
    # borrados masivos de conversaciones.
    for tid in (t1, t2):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    Customer.__table__.delete().where(Customer.external_id.like("rls:%"))
                )

    async with tenant_scope(t1):
        async with get_session() as session:
            session.add(Customer(tenant_id=t1, channel="whatsapp", external_id="rls:t1"))
    async with tenant_scope(t2):
        async with get_session() as session:
            session.add(Customer(tenant_id=t2, channel="whatsapp", external_id="rls:t2"))

    # sin contexto de tenant (rol de runtime) -> 0 filas de cualquier tenant
    async with get_session() as session:
        n = (await session.execute(select(func.count()).select_from(Customer))).scalar()
    assert n == 0, f"sin contexto debería ver 0 filas, vio {n}"

    # scope T1 -> ve su cliente, NO el de T2
    async with tenant_scope(t1):
        async with get_session() as session:
            ids = (await session.execute(select(Customer.external_id))).scalars().all()
    assert "rls:t1" in ids, f"scope T1 debería ver su cliente: {ids}"
    assert "rls:t2" not in ids, f"scope T1 NO debería ver el de T2: {ids}"

    # T1 intentando leer por tenant_id de T2 -> RLS lo impide
    async with tenant_scope(t1):
        async with get_session() as session:
            leaked = (
                await session.execute(
                    select(func.count()).select_from(Customer).where(Customer.tenant_id == t2)
                )
            ).scalar()
    assert leaked == 0, f"RLS debió bloquear la lectura de T2 desde T1, vio {leaked}"


# ------------------------------------------------------ 5. recovery de huérfanas
async def test_recover_orphaned_conversations_siembra_tenant_id(tenant_setup):
    """Bug encontrado por `auditor-multitenant` (2026-09-12) sobre el fix de
    debounce: el `for` que recorría las conversaciones huérfanas quedaba
    (por indentación) FUERA del `tenant_scope` que las había consultado --
    `schedule_debounce` llamaba `get_current_tenant_id()` y obtenía `None`,
    así que el payload nunca llevaba `tenant_id` y `_fire_conversation` lo
    descartaba en silencio. La recuperación de crash quedaba inoperante
    para TODOS los tenants. Este test reproduce el escenario mínimo: una
    conversación abierta, sin timer vivo, cuyo último mensaje es inbound.
    """
    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session
    from core.memory.working import get_redis
    from core.orchestrator.debounce import PAYLOAD_KEY, PENDING_KEY, recover_orphaned_conversations
    from core.shared.tenant import tenant_scope

    t1, _ = tenant_setup
    r = get_redis()
    external_id = f"orphan:test:{uuid.uuid4().hex[:8]}"

    async with tenant_scope(t1):
        async with get_session() as session:
            customer = Customer(tenant_id=t1, channel="whatsapp", external_id=external_id)
            session.add(customer)
            await session.flush()
            conversation = Conversation(tenant_id=t1, customer_id=customer.id, status="open")
            session.add(conversation)
            await session.flush()
            session.add(
                Message(
                    tenant_id=t1,
                    conversation_id=conversation.id,
                    direction="inbound",
                    sender_type="customer",
                    content="mensaje huérfano",
                )
            )
            conversation_id = conversation.id

    try:
        await recover_orphaned_conversations()

        raw_payload = await r.get(PAYLOAD_KEY.format(conversation_id=conversation_id))
        assert raw_payload is not None, "recover_orphaned_conversations no programó ningún timer"
        payload = json.loads(raw_payload)
        assert payload.get("tenant_id") == str(t1), (
            f"tenant_id no sembrado en el payload recuperado: {payload!r}"
        )
    finally:
        from core.orchestrator.debounce import MESSAGES_KEY

        await r.delete(
            PAYLOAD_KEY.format(conversation_id=conversation_id),
            MESSAGES_KEY.format(conversation_id=conversation_id),
        )
        await r.zrem(PENDING_KEY, str(conversation_id))
        async with tenant_scope(t1):
            async with get_session() as session:
                await session.execute(
                    Message.__table__.delete().where(Message.conversation_id == conversation_id)
                )
                await session.execute(
                    Conversation.__table__.delete().where(Conversation.id == conversation_id)
                )
                await session.execute(
                    Customer.__table__.delete().where(Customer.id == customer.id)
                )