"""Gate de turnos con presupuesto acotado (HU-ARCH-27).

Puros de asyncio: los contadores Redis son best-effort (sin Redis, se saltan).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from core.orchestrator import turn_gate


@pytest.fixture(autouse=True)
def _settings_rapidos(monkeypatch: pytest.MonkeyPatch):
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "turn_timeout_s", 1)
    monkeypatch.setattr(settings, "turn_queue_timeout_s", 1)
    monkeypatch.setattr(settings, "turn_slow_warn_s", 3600)
    return settings


@pytest.mark.asyncio
async def test_turno_lento_se_corta_y_libera_el_cupo() -> None:
    async def lento() -> str:
        await asyncio.sleep(30)
        return "nunca"

    tenant = uuid.uuid4()
    with pytest.raises(turn_gate.TurnTimeout):
        await turn_gate.run_turn(tenant, lento())

    # El semáforo quedó libre: un turno rápido del MISMO tenant corre enseguida.
    async def rapido() -> str:
        return "ok"

    assert await turn_gate.run_turn(tenant, rapido()) == "ok"


@pytest.mark.asyncio
async def test_cupo_saturado_no_espera_para_siempre(monkeypatch: pytest.MonkeyPatch) -> None:
    # Los turnos retenidos no deben cortarse por timeout durante el test.
    monkeypatch.setattr(turn_gate.get_settings(), "turn_timeout_s", 30)
    tenant = uuid.uuid4()
    liberar = asyncio.Event()

    async def colgado() -> str:
        await liberar.wait()
        return "tarde"

    # Ocupa los 2 slots del cupo por tenant (default).
    t1 = asyncio.create_task(turn_gate.run_turn(tenant, colgado()))
    t2 = asyncio.create_task(turn_gate.run_turn(tenant, colgado()))
    await asyncio.sleep(0.05)

    ejecutado = False

    async def no_debe_correr() -> str:
        nonlocal ejecutado
        ejecutado = True
        return "no"

    with pytest.raises(turn_gate.TurnQueueTimeout):
        await turn_gate.run_turn(tenant, no_debe_correr())
    assert ejecutado is False, "el turno no debe ejecutarse si no consiguió cupo"

    liberar.set()
    await asyncio.gather(t1, t2)


@pytest.mark.asyncio
async def test_tenant_saturado_no_bloquea_a_otro(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turn_gate.get_settings(), "turn_timeout_s", 30)
    a, b = uuid.uuid4(), uuid.uuid4()
    liberar = asyncio.Event()

    async def colgado() -> str:
        await liberar.wait()
        return "tarde"

    t1 = asyncio.create_task(turn_gate.run_turn(a, colgado()))
    t2 = asyncio.create_task(turn_gate.run_turn(a, colgado()))
    await asyncio.sleep(0.05)

    async def rapido() -> str:
        return "b-ok"

    # El cupo de A está lleno, pero B tiene su propio cupo.
    assert await turn_gate.run_turn(b, rapido()) == "b-ok"

    liberar.set()
    await asyncio.gather(t1, t2)
