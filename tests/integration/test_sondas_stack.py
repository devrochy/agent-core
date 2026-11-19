"""Sondas de métricas contra el Postgres y el Redis reales (B3, ADR-0188)."""

from __future__ import annotations

import os

import pytest

import core.observability.sondas as sondas

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def test_una_vuelta_de_sondas_con_el_stack_real(monkeypatch):
    from core.orchestrator.consumer import ensure_group

    valores = {"dlq": None, "inbound": None, "latido": None, "deps": {}, "crons": {}}
    monkeypatch.setattr(sondas, "_valores", valores)
    await ensure_group()
    await sondas.medir()
    assert valores["deps"] == {"db": 1, "redis": 1}
    assert isinstance(valores["dlq"], int) and isinstance(valores["inbound"], int)
    assert isinstance(valores["latido"], float)
    assert isinstance(valores["crons"], dict)  # la consulta de `cron_runs` corre
