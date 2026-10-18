"""Un pedido de comida en un negocio que también agenda no escala por "tenemos disponibilidad".

Reproduce con el loop real y un modelo guionado (sin LLM) la falla de `pedido-a-domicilio` en la
primera medición de pass^4 de los procedimientos (2026-10-18): con `pack_commerce` y `pack_calendar`
en el mismo negocio, el `calendar_completion_check` leía "¡Sí, tenemos disponibilidad!" (stock de
una hamburguesa) como un horario libre sin consultar la agenda, bloqueaba dos veces y el turno
escalaba ("ya mismo te comunico con una persona"). Con un dato de agenda en la oración sigue
bloqueando, y el motivo queda en `audit_log` (`core:guardrail`) para el reporte de los journeys.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RAIZ = Path(__file__).resolve().parents[2]
if "journeys" not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location("journeys", RAIZ / "evals" / "journeys.py")
    assert _SPEC and _SPEC.loader
    _modulo = importlib.util.module_from_spec(_SPEC)
    sys.modules["journeys"] = _modulo
    _SPEC.loader.exec_module(_modulo)
journeys = sys.modules["journeys"]


class _Guion:
    """Modelo guionado: un paso por llamada, texto o `(tool, args)`."""

    provider_name = "fake"
    model = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Any]) -> None:
        self.pasos = list(pasos)
        self.llamadas = 0

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMToolCall

        self.llamadas += 1
        paso = self.pasos.pop(0) if self.pasos else "Listo."
        uso = types.SimpleNamespace(input_tokens=5, output_tokens=5)
        if isinstance(paso, tuple):
            nombre, args = paso
            return types.SimpleNamespace(
                content="", stop_reason="tool_use", model="fake", latency_ms=1, usage=uso,
                tool_calls=[LLMToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=nombre, arguments=dict(args))],
            )
        return types.SimpleNamespace(content=paso, stop_reason="end_turn", model="fake", latency_ms=1, usage=uso,
                                     tool_calls=[])


@pytest.fixture(scope="module")
async def negocio() -> AsyncIterator[tuple[uuid.UUID, Any]]:
    """El negocio del set `procedimientos` (commerce + payments + calendar), sin el motor."""
    import pack_payments.payments as capacidad

    from core.tenants import deactivate_tenant, purge_tenant

    original = capacidad.build_provider
    await journeys._preparar_proceso()
    definicion, _rutas = journeys.rutas_del_set("procedimientos", [""])
    pagos = journeys.PagoSimulado(monto_minimo=150000)
    slug = f"pedagenda-{uuid.uuid4().hex[:8]}"
    tid = await journeys.preparar_negocio(definicion, pagos, slug=slug, motor=False)
    yield tid, pagos
    capacidad.build_provider = original
    await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _turno(negocio: tuple[uuid.UUID, Any], monkeypatch: Any, mensaje: str, pasos: list[Any]) -> tuple[Any, Any]:
    tid, pagos = negocio
    customer_id, conversation_id = await journeys.nuevo_cliente(tid, "Ana")
    ctx = journeys.Contexto(tid, customer_id, conversation_id, pagos)
    guion = _Guion(pasos)
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    return await journeys._turno(ctx, mensaje), ctx


async def test_tenemos_disponibilidad_de_un_producto_no_escala(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    inicio = datetime.now(UTC) - timedelta(seconds=1)
    resultado, ctx = await _turno(
        negocio,
        monkeypatch,
        "Hola, quiero pedir dos hamburguesas a domicilio, pago por transferencia.",
        [
            ("consultar_inventario", {"query": "hamburguesa"}),
            "¡Sí, tenemos disponibilidad! La Hamburguesa doble carne es la que tenemos hoy.",
        ],
    )
    assert resultado is not None and not resultado.escalated, resultado
    assert "tenemos disponibilidad" in resultado.text
    assert await journeys.motivo_del_relevo(ctx, inicio) == ""


async def test_disponibilidad_con_dato_de_agenda_sigue_bloqueando_y_deja_el_motivo(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime, timedelta

    inicio = datetime.now(UTC) - timedelta(seconds=1)
    afirmacion = "Tenemos disponibilidad el jueves a las 3 pm."
    resultado, ctx = await _turno(negocio, monkeypatch, "¿Tienen espacio el jueves?", [afirmacion, afirmacion])
    assert resultado is not None and resultado.escalated
    motivo = await journeys.motivo_del_relevo(ctx, inicio)
    assert motivo == "guardrail=calendar_completion_check (disponibilidad)", motivo
    assert "jueves" not in motivo  # solo el identificador del guardrail, nunca el borrador


async def test_el_reporte_lee_el_runtime_efectivo(negocio) -> None:  # type: ignore[no-untyped-def]
    tid, _pagos = negocio
    assert await journeys.runtime_efectivo(tid) == {"aos_runtime": "legacy", "aos_procedures": False}
