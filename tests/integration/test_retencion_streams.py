"""Retención de `agent_core:inbound` y su DLQ (ADR-0053).

Lo procesado se borra al ackear (`XDEL`) y el recorte periódico (`XTRIM MINID`)
quita lo ackeado viejo, sin tocar nunca lo pendiente (PEL) ni lo no leído. La DLQ
se recorta por antigüedad y la purga de un negocio borra sus entradas de la DLQ.
Requiere Redis real (y Postgres por los fixtures del conftest); no usa el LLM.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
from conftest import PHONE, TEST_ALPHA_ID

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

HORA_MS = 3_600_000


def _fields(tenant_id: uuid.UUID | str = TEST_ALPHA_ID, content: str = "hola") -> dict[str, str]:
    return {
        "tenant_id": str(tenant_id),
        "provider": "meta",
        "channel": "whatsapp",
        "external_message_id": f"wamid.RET.{uuid.uuid4().hex[:8]}",
        "customer_external_id": "whatsapp:+573000000000",
        "customer_name": "Test",
        "content": content,
        "connection_external_id": PHONE,
        "media": "[]",
        "media_context": "",
    }


def _stream() -> str:
    # Prefijo `agent_core:` para que el fixture `_clean_redis` lo limpie.
    return f"agent_core:test:retencion:{uuid.uuid4().hex[:8]}"


async def _ids(r, stream: str) -> list[str]:
    return [eid for eid, _ in await r.xrange(stream)]


async def _pendientes(r, stream: str, group: str) -> list[str]:
    return [p["message_id"] for p in await r.xpending_range(stream, group, min="-", max="+", count=100)]


@pytest.fixture
def consumer_aislado(monkeypatch):
    """Consumer con streams propios y un turno que siempre se procesa (sin BD ni LLM)."""
    from core.orchestrator import consumer as mod

    inbound, dlq = _stream(), _stream()
    monkeypatch.setattr(mod, "INBOUND_STREAM", inbound)
    monkeypatch.setattr(mod, "DEAD_LETTER_STREAM", dlq)
    resultado = {"valor": mod.TURN_PROCESSED}

    async def handle_inbound(_fields: dict[str, str]) -> str:
        return resultado["valor"]

    async def run_turn(_tenant_id, coro):
        return await coro

    async def revoke_claim(_fields: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(mod, "handle_inbound", handle_inbound)
    monkeypatch.setattr(mod, "run_turn", run_turn)
    monkeypatch.setattr(mod, "_revoke_claim", revoke_claim)
    return mod, inbound, dlq, resultado


async def test_ack_borra_lo_procesado_y_no_lo_pendiente(consumer_aislado) -> None:
    from core.memory.working import get_redis

    mod, inbound, _dlq, resultado = consumer_aislado
    r = get_redis()
    await r.xgroup_create(inbound, mod.GROUP, id="0", mkstream=True)
    procesado = await r.xadd(inbound, _fields(content="procesado"))
    ocupado = await r.xadd(inbound, _fields(content="otro worker"))
    sin_leer = await r.xadd(inbound, _fields(content="sin leer"))
    await r.xreadgroup(mod.GROUP, mod.CONSUMER, {inbound: ">"}, count=2)

    await mod._handle_entry(procesado, _fields())
    resultado["valor"] = mod.TURN_BUSY
    await mod._handle_entry(ocupado, _fields())

    # Lo procesado ya no está (ni texto ni teléfono); lo pendiente y lo no leído, sí.
    assert await _ids(r, inbound) == [ocupado, sin_leer]
    assert await _pendientes(r, inbound, mod.GROUP) == [ocupado]


async def test_dead_letter_borra_del_stream_y_conserva_en_dlq(consumer_aislado) -> None:
    from core.memory.working import get_redis

    mod, inbound, dlq, _ = consumer_aislado
    r = get_redis()
    await r.xgroup_create(inbound, mod.GROUP, id="0", mkstream=True)
    fields = _fields(content="falla")
    entry_id = await r.xadd(inbound, fields)
    await r.xreadgroup(mod.GROUP, mod.CONSUMER, {inbound: ">"}, count=1)

    await mod._dead_letter(r, entry_id, fields, 3, error="RuntimeError: x")

    assert await _ids(r, inbound) == []
    assert await _pendientes(r, inbound, mod.GROUP) == []
    filas = await r.xrange(dlq)
    assert [f["content"] for _eid, f in filas] == ["falla"]


async def test_recorte_respeta_pel_y_no_leidos() -> None:
    """XTRIM MINID nunca pasa del pendiente más viejo ni del último entregado."""
    from core.memory.working import get_redis
    from core.orchestrator.retencion_streams import recortar_inbound

    r = get_redis()
    s, g = _stream(), "grupo"
    viejo = int(time.time() * 1000) - 48 * HORA_MS
    await r.xgroup_create(s, g, id="0", mkstream=True)
    e1, e2, e3, e4 = [await r.xadd(s, _fields(), id=f"{viejo + i}-0") for i in range(1, 5)]
    e5 = await r.xadd(s, _fields(content="reciente"))
    await r.xreadgroup(g, "c1", {s: ">"}, count=3)  # entrega e1, e2, e3
    await r.xack(s, g, e1, e3)  # ackeados sin XDEL (p. ej. antes de este cambio)

    # e2 sigue pendiente: solo se puede recortar lo anterior a él.
    assert await recortar_inbound(r, s, 24) == 1
    assert await _ids(r, s) == [e2, e3, e4, e5]
    assert await _pendientes(r, s, g) == [e2]

    # Ackeado e2, se recortan e2 y e3; e4 (viejo pero no leído) se queda.
    await r.xack(s, g, e2)
    assert await recortar_inbound(r, s, 24) == 2
    assert await _ids(r, s) == [e4, e5]

    # Leído y ackeado todo: e4 (viejo) se va; e5 (reciente, < 24 h) se queda.
    await r.xreadgroup(g, "c1", {s: ">"}, count=10)
    await r.xack(s, g, e4, e5)
    assert await recortar_inbound(r, s, 24) == 1
    assert await _ids(r, s) == [e5]
    # Idempotente.
    assert await recortar_inbound(r, s, 24) == 0


async def test_pendiente_atascado_frena_el_recorte_y_avisa(monkeypatch) -> None:
    """Un pendiente más viejo que dos retenciones no se borra, pero se registra el bloqueo."""
    from core.memory.working import get_redis
    from core.orchestrator import retencion_streams

    avisos: list[tuple[str, dict]] = []

    class _Log:
        def warning(self, evento: str, **kw) -> None:
            avisos.append((evento, kw))

    monkeypatch.setattr(retencion_streams, "log", _Log())
    r = get_redis()
    s, g = _stream(), "grupo"
    ahora = int(time.time() * 1000)
    await r.xgroup_create(s, g, id="0", mkstream=True)
    atascado = await r.xadd(s, _fields(), id=f"{ahora - 72 * HORA_MS}-0")
    procesado = await r.xadd(s, _fields(), id=f"{ahora - 60 * HORA_MS}-0")
    await r.xreadgroup(g, "c", {s: ">"}, count=10)
    await r.xack(s, g, procesado)

    assert await retencion_streams.recortar_inbound(r, s, 24) == 0
    assert await _ids(r, s) == [atascado, procesado]
    assert [e for e, _ in avisos] == ["inbound_trim_blocked_by_pending"]
    assert avisos[0][1]["corte_id"] == atascado
    # Solo IDs y números: nada de texto ni teléfono.
    assert set(avisos[0][1]) == {"stream", "corte_id", "horas_atras"}


async def test_recorte_con_varios_grupos_respeta_el_mas_atrasado() -> None:
    from core.memory.working import get_redis
    from core.orchestrator.retencion_streams import recortar_inbound

    r = get_redis()
    s = _stream()
    viejo = int(time.time() * 1000) - 48 * HORA_MS
    await r.xgroup_create(s, "rapido", id="0", mkstream=True)
    await r.xgroup_create(s, "lento", id="0")
    ids = [await r.xadd(s, _fields(), id=f"{viejo + i}-0") for i in range(1, 4)]
    await r.xreadgroup("rapido", "c", {s: ">"}, count=10)
    await r.xack(s, "rapido", *ids)
    await r.xreadgroup("lento", "c", {s: ">"}, count=1)
    await r.xack(s, "lento", ids[0])

    assert await recortar_inbound(r, s, 24) == 1
    assert await _ids(r, s) == ids[1:]


async def test_sin_grupo_o_sin_stream_no_se_recorta() -> None:
    from core.memory.working import get_redis
    from core.orchestrator.retencion_streams import recortar_inbound

    r = get_redis()
    s = _stream()
    assert await recortar_inbound(r, s, 1) == 0
    viejo = int(time.time() * 1000) - 48 * HORA_MS
    await r.xadd(s, _fields(), id=f"{viejo}-0")
    # Sin grupo nada consta como procesado.
    assert await recortar_inbound(r, s, 1) == 0
    assert len(await _ids(r, s)) == 1


async def test_recorte_periodico_de_stream_y_dlq(monkeypatch) -> None:
    """`_recortar_streams` aplica las retenciones configuradas a ambos streams."""
    from core.config import get_settings
    from core.memory.working import get_redis
    from core.orchestrator import consumer as mod

    r = get_redis()
    inbound, dlq = _stream(), _stream()
    monkeypatch.setattr(mod, "INBOUND_STREAM", inbound)
    monkeypatch.setattr(mod, "DEAD_LETTER_STREAM", dlq)
    monkeypatch.setattr(get_settings(), "inbound_stream_retention_hours", 24)
    monkeypatch.setattr(get_settings(), "inbound_dlq_retention_hours", 168)
    ahora = int(time.time() * 1000)

    await r.xgroup_create(inbound, mod.GROUP, id="0", mkstream=True)
    procesado = await r.xadd(inbound, _fields(), id=f"{ahora - 48 * HORA_MS}-0")
    pendiente = await r.xadd(inbound, _fields(), id=f"{ahora - 47 * HORA_MS}-0")
    await r.xreadgroup(mod.GROUP, "c", {inbound: ">"}, count=10)
    await r.xack(inbound, mod.GROUP, procesado)

    vencida = await r.xadd(dlq, _fields(), id=f"{ahora - 200 * HORA_MS}-0")
    vigente = await r.xadd(dlq, _fields(), id=f"{ahora - 100 * HORA_MS}-0")

    await mod._recortar_streams(r)

    assert await _ids(r, inbound) == [pendiente]
    assert await _ids(r, dlq) == [vigente]
    assert vencida not in await _ids(r, dlq)


@pytest.mark.aislamiento
async def test_purga_borra_dlq_solo_del_tenant(monkeypatch) -> None:
    """La limpieza Redis de un negocio purgado borra SUS entradas de la DLQ, no las de otro."""
    from core.memory.working import get_redis
    from core.orchestrator import consumer as mod
    from core.orchestrator import retencion_streams
    from core.tenants import purge_tenant_redis

    r = get_redis()
    dlq = _stream()
    monkeypatch.setattr(mod, "DEAD_LETTER_STREAM", dlq)
    monkeypatch.setattr(retencion_streams, "_LOTE_DLQ", 2)  # ejercita la paginación
    purgado = uuid.uuid4()  # no existe en la base: equivale a un negocio ya purgado
    for _ in range(3):
        await r.xadd(dlq, _fields(purgado, content="del purgado"))
    # Mismo UUID en mayúsculas: también es suyo.
    await r.xadd(dlq, _fields(str(purgado).upper(), content="del purgado"))
    ajena = await r.xadd(dlq, _fields(TEST_ALPHA_ID, content="de otro negocio"))
    invalida = await r.xadd(dlq, _fields("no-es-un-uuid", content="sin tenant válido"))

    assert await purge_tenant_redis(purgado) == 4
    filas = await r.xrange(dlq)
    assert [eid for eid, _ in filas] == [ajena, invalida]
    assert filas[0][1]["tenant_id"] == str(TEST_ALPHA_ID)
    assert await purge_tenant_redis(purgado) == 0, "idempotente"
