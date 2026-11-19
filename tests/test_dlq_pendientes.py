"""HU-QA-13: las entradas de la DLQ que siembra el escenario «Mensajes fallidos» no cuentan para la
alerta al on-call ni para el chequeo previo a producción (`dlq_pendientes`)."""

from __future__ import annotations

import asyncio
from typing import Any

from core.orchestrator.consumer import DEAD_LETTER_STREAM, MARCA_ESCENARIO, dlq_pendientes


class _Redis:
    def __init__(self, entradas: list[dict[Any, Any]]) -> None:
        self.entradas = entradas

    async def xlen(self, stream: str) -> int:
        assert stream == DEAD_LETTER_STREAM
        return len(self.entradas)

    async def xrange(self, stream: str, count: int) -> list[tuple[str, dict[Any, Any]]]:
        assert stream == DEAD_LETTER_STREAM
        return [(f"{n}-0", e) for n, e in enumerate(self.entradas[:count])]


def test_cuenta_solo_los_fallos_reales() -> None:
    reales = [{"tenant_id": "a"}, {b"tenant_id": b"b"}]
    sembradas = [{"tenant_id": "c", MARCA_ESCENARIO: "mensajes-fallidos"}, {MARCA_ESCENARIO.encode(): b"x"}]
    assert asyncio.run(dlq_pendientes(_Redis(reales + sembradas))) == 2
    assert asyncio.run(dlq_pendientes(_Redis(sembradas))) == 0
    assert asyncio.run(dlq_pendientes(_Redis([]))) == 0
