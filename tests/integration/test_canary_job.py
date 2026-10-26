"""HU-AOS-32 parte 2 · el job del canary: señal dura, no-inferioridad, avisos e idempotencia.

Sin LLM ni Celery: se llama a `canary_job.revisar(ahora=...)` (como `test_cliente_esperando`) y la
muestra se siembra en `core_decisions` / `turn_outcomes` / `usage_ledger` después de iniciar el canary.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_canary import _con_candidata, _svc
from test_releases_por_conexion import _activa, _conversacion, _negocio, _release_uno, _sql

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

LUEGO = datetime.now(UTC) + timedelta(days=4)


async def _iniciado() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, datetime]:
    tid, cid, uno, dos = await _con_candidata()
    await _svc(tid, "iniciar", cid, dos, percent=50, actor="test")
    ((inicio,),) = await _sql(tid, "select created_at from release_events where release_id = :r and type = 'canary_started'", r=dos)
    return tid, cid, uno, dos, inicio


async def _sembrar(
    tid: uuid.UUID, cid: uuid.UUID, rid: uuid.UUID, lane: str, n: int, desde: datetime, *,
    escaladas: int = 0, fallidas: int = 0, costo: float = 0.0,
) -> None:
    for i in range(n):
        conv = await _conversacion(tid, cid)
        turno = uuid.uuid4()
        await _sql(
            tid,
            "insert into core_decisions (id, tenant_id, connection_id, conversation_id, release_id, release_lane, outcome, latency_ms, created_at) "
            "values (:id, :t, :c, :conv, :r, :l, :o, 5, :w)",
            id=turno, t=tid, c=cid, conv=conv, r=rid, l=lane, o="escalated" if i < escaladas else "routed",
            w=desde + timedelta(minutes=1 + i),
        )
        await _sql(
            tid,
            "insert into turn_outcomes (turn_id, tenant_id, status, latency_ms) values (:id, :t, :s, 900)",
            id=turno, t=tid, s="failed" if i < fallidas else ("escalated" if i < escaladas else "replied"),
        )
        if costo:
            await _sql(
                tid,
                "insert into usage_ledger (id, tenant_id, conversation_id, model, input_tokens, output_tokens, cost_usd, kind, "
                "turn_id, connection_id, release_id) values (:id, :t, :conv, 'fake', 1, 1, :costo, 'turn', :turno, :c, :r)",
                id=uuid.uuid4(), t=tid, conv=conv, costo=costo, turno=turno, c=cid, r=rid,
            )


async def _revisar(no_inferioridad: bool, ahora: datetime = LUEGO) -> dict[str, int]:
    from core.releases.canary_job import revisar

    return await revisar(ahora, no_inferioridad=no_inferioridad)


async def _avisos(tid: uuid.UUID, event_key: str) -> list[Any]:
    return await _sql(tid, "select severity, data->>'reason' from notifications where event_key = :k", k=event_key)


async def _parado(tid: uuid.UUID, rid: uuid.UUID) -> list[Any]:
    return await _sql(tid, "select data->>'reason' from release_events where release_id = :r and type = 'canary_stopped'", r=rid)


async def test_senal_dura_retira_y_bloquea_una_sola_vez() -> None:
    tid, cid, uno, dos, inicio = await _iniciado()
    await _sembrar(tid, cid, uno, "stable", 30, inicio)
    await _sembrar(tid, cid, dos, "canary", 20, inicio, fallidas=8)
    await _revisar(no_inferioridad=False)
    assert await _parado(tid, dos) == [("senal_dura",)]
    assert await _sql(tid, "select canary_release_id from channel_connections where id = :c", c=cid) == [(None,)]
    assert await _avisos(tid, "core.canary_revertido") == [("critical", "senal_dura")]
    # Bloqueada: no se vuelve a probar. Una segunda corrida no duplica nada.
    from core.releases.promocion import ConflictoDeRelease

    with pytest.raises(ConflictoDeRelease) as exc:
        await _svc(tid, "iniciar", cid, dos, percent=10, actor="test")
    assert exc.value.codigo == "release_retirada"
    await _revisar(no_inferioridad=False)
    assert len(await _parado(tid, dos)) == 1 and len(await _avisos(tid, "core.canary_revertido")) == 1


async def test_peor_se_decide_cada_hora_y_retira() -> None:
    tid, cid, uno, dos, inicio = await _iniciado()
    await _sembrar(tid, cid, uno, "stable", 210, inicio, escaladas=10)
    await _sembrar(tid, cid, dos, "canary", 210, inicio, escaladas=40)
    await _revisar(no_inferioridad=False)  # la de 5 min no mira la no-inferioridad
    assert await _parado(tid, dos) == []
    await _revisar(no_inferioridad=True)
    assert await _parado(tid, dos) == [("no_inferioridad",)]
    assert await _sql(tid, "select data->>'veredicto' from release_events where release_id = :r and type = 'canary_evaluated'", r=dos) == [("peor",)]
    assert await _avisos(tid, "core.canary_revertido") == [("critical", "no_inferioridad")]


async def test_listo_avisa_una_vez_y_el_costo_aparte() -> None:
    tid, cid, uno, dos, inicio = await _iniciado()
    await _sembrar(tid, cid, uno, "stable", 210, inicio, escaladas=20, costo=0.010)
    await _sembrar(tid, cid, dos, "canary", 210, inicio, escaladas=20, costo=0.013)
    await _revisar(no_inferioridad=True)
    await _revisar(no_inferioridad=True)
    assert await _avisos(tid, "core.canary_listo") == [("info", None)]
    assert await _avisos(tid, "core.canary_costo") == [("warning", None)]
    # Sigue vivo y se puede publicar.
    assert await _sql(tid, "select canary_release_id from channel_connections where id = :c", c=cid) == [(dos,)]
    cambio = await _svc(tid, "publicar", cid, actor="test", ahora=LUEGO)
    assert cambio.to_release_id == dos and await _activa(tid, cid) == dos


async def test_siete_dias_sin_muestra_avisa_una_vez() -> None:
    tid, _cid, _uno, dos, _inicio = await _iniciado()
    await _revisar(no_inferioridad=True, ahora=datetime.now(UTC) + timedelta(days=3))
    assert await _avisos(tid, "core.canary_sin_muestra") == []
    for _ in range(2):
        await _revisar(no_inferioridad=True, ahora=datetime.now(UTC) + timedelta(days=8))
    assert await _avisos(tid, "core.canary_sin_muestra") == [("warning", None)]
    assert await _parado(tid, dos) == []


async def test_habilidad_retirada_detiene_sin_bloquear(monkeypatch: Any) -> None:
    from core.releases import canary_job

    tid, _cid, _uno, dos, _inicio = await _iniciado()

    async def _retirada(_session: Any, t: uuid.UUID, r: uuid.UUID) -> bool:
        return t == tid and r == dos

    monkeypatch.setattr(canary_job, "_usa_habilidad_retirada", _retirada)
    await _revisar(no_inferioridad=False)
    assert await _parado(tid, dos) == [("habilidad_retirada",)]
    assert await _avisos(tid, "core.canary_revertido") == [("critical", "habilidad_retirada")]
    from core.db.session import get_session
    from core.releases.promocion import bloqueada, ultimo_ciclo
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            assert not bloqueada(*await ultimo_ciclo(s, tid, dos))


async def test_un_negocio_no_ve_el_de_otro_y_legacy_se_salta() -> None:
    from core.releases.build import ConexionNoEncontrada
    from core.releases.canary_job import revisar_conexion

    tid, cid, uno, dos, inicio = await _iniciado()
    otro, _s, cid_otro = await _negocio()
    await _release_uno(otro)  # legacy: el job no lo recorre
    await _sembrar(tid, cid, uno, "stable", 30, inicio)
    await _sembrar(tid, cid, dos, "canary", 20, inicio, fallidas=8)
    from core.shared.tenant import tenant_scope

    # La conexión de un negocio no existe bajo el scope de otro.
    async with tenant_scope(otro):
        with pytest.raises(ConexionNoEncontrada):
            await revisar_conexion(otro, cid, ahora=LUEGO, no_inferioridad=False)
    await _revisar(no_inferioridad=False)
    assert await _parado(tid, dos) == [("senal_dura",)]
    assert await _sql(otro, "select count(*) from notifications") == [(0,)]
    del cid_otro
