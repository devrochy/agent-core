"""Conversación ordenada con el motor (adenda 2026-10-13 de ADR-0113 y de ADR-0125).

Los recorridos de la segunda prueba real en test (chat web, 2026-10-13: datos pedidos de a tandas, comandos y
negaciones que escalaban en falso, el resumen único y el eco de la confirmación) se reproducían con
`pack_commerce.contratar_plan`. Ese procedimiento salió de commerce (SPEC-43, HU-COM-17): sus casos viven ahora en
`test_motor_contratar_plan.py`, sobre `pack_subscriptions.contratar_plan`. Aquí queda lo que no depende del
recorrido: con el motor en sombra nada cambia y el tope de vueltas escala como siempre.
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
from sqlalchemy import text

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

CHECKOUT = {"recoger_en_tienda": ["full_name", "phone"]}
CATALOGO = {
    "pack": "pack_commerce",
    "category_paths": [["Comida"]],
    "products": [
        {"name": "Hamburguesa", "description": "Doble carne.", "category_path": ["Comida"],
         "variants": [{"sku": "HAM-1", "price": 25000, "stock": 500, "attributes": {}}]},
    ],
}


async def _banderas(tid: uuid.UUID, runtime: str, procedimientos: bool) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("update tenants set aos_procedures = false where id = :t"), {"t": tid})
            await conn.execute(
                text("update tenants set aos_runtime = :r, aos_procedures = :p where id = :t"),
                {"t": tid, "r": runtime, "p": procedimientos},
            )
    finally:
        await engine.dispose()


async def _negocio(slug: str, pagos: Any) -> uuid.UUID:
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tid = await journeys.preparar_negocio(
        {
            "nombre": "Vendedor de planes",
            "packs": ["pack_commerce", "pack_payments"],
            "plan": "premium",
            "config": {
                "pack_payments": {"public_key": f"pub_test_{slug}", "private_key": f"prv_test_{slug}",
                                  "events_secret": f"test_events_{slug}", "sandbox": True},
            },
            "catalogo": CATALOGO,
        },
        pagos,
        slug=slug,
    )
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_commerce", {"checkout_fields": CHECKOUT})
    await _banderas(tid, "v3", True)
    return tid


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, Any]]:
    """Dos negocios con el motor: A vende planes de la plataforma (términos publicados), B no."""
    import pack_payments.payments as capacidad

    from core.tenants import deactivate_tenant, purge_tenant

    original = capacidad.build_provider
    await journeys._preparar_proceso()
    pagos = journeys.PagoSimulado(monto_minimo=150000)
    slugs = [f"ordenada-a-{uuid.uuid4().hex[:6]}", f"ordenada-b-{uuid.uuid4().hex[:6]}"]
    a = await _negocio(slugs[0], pagos)
    b = await _negocio(slugs[1], pagos)
    await journeys.publicar_documentos_legales(a, [
        {"tipo": t, "version": "ordenada", "url": f"https://solvershub.test/legal/{t}"}
        for t in ("terminos", "privacidad", "dpa")
    ])
    yield a, b, pagos
    capacidad.build_provider = original
    for tid, slug in zip((a, b), slugs, strict=True):
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


Paso = Any  # texto, (tool, args) o una función de los resultados anteriores que devuelve texto


class _Guion:
    """Modelo guionado: un paso por llamada. Guarda las tools ofrecidas, el prompt de sistema y el
    resultado de la tool anterior de cada llamada. Sin pasos, repite `por_defecto`."""

    provider_name = "fake"
    model = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Paso], por_defecto: Paso = "Listo.") -> None:
        self.pasos = list(pasos)
        self.por_defecto = por_defecto
        self.tools: list[list[str]] = []
        self.system: list[str] = []
        self.resultados: list[str] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMToolCall

        self.tools.append(sorted(t.name for t in kwargs.get("tools") or []))
        self.system.append("\n".join(str(b) for b in kwargs.get("system_blocks") or []))
        mensajes = kwargs.get("messages") or []
        if mensajes and mensajes[-1].role == "tool":
            self.resultados.append(str(mensajes[-1].content))
        paso = self.pasos.pop(0) if self.pasos else self.por_defecto
        if callable(paso):
            paso = paso(self.resultados)
        uso = types.SimpleNamespace(input_tokens=5, output_tokens=5)
        if isinstance(paso, tuple):
            nombre, args = paso
            return types.SimpleNamespace(
                content="", stop_reason="tool_use", model="fake", latency_ms=1, usage=uso,
                tool_calls=[LLMToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=nombre, arguments=dict(args))],
            )
        return types.SimpleNamespace(content=paso, stop_reason="end_turn", model="fake", latency_ms=1, usage=uso,
                                     tool_calls=[])


async def _turno(ctx: Any, monkeypatch: Any, mensaje: str, pasos: list[Paso], por_defecto: Paso = "Listo.") -> tuple[Any, _Guion]:
    guion = _Guion(pasos, por_defecto)
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    resultado = await journeys._turno(ctx, mensaje)
    return resultado, guion


async def _cliente(tid: uuid.UUID, pagos: Any, nombre: str = "Rob") -> Any:
    customer_id, conversation_id = await journeys.nuevo_cliente(tid, nombre)
    return journeys.Contexto(tid, customer_id, conversation_id, pagos)


async def _filas(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list((await s.execute(text(sql), {"t": tid, **params})).all())


async def _tarea(ctx: Any) -> Any:
    filas = await _filas(
        ctx.tenant_id,
        "select id, stage, status, slots, pending_confirmation from conversation_tasks "
        "where tenant_id = :t and conversation_id = :c order by created_at desc limit 1",
        c=ctx.conversation_id,
    )
    return filas[0] if filas else None


def _valor(tarea: Any, dato: str) -> Any:
    return (tarea.slots.get(dato) or {}).get("value")


async def test_en_sombra_nada_cambia_y_el_tope_escala_como_siempre(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Con `aos_runtime = v3` y el motor apagado (sombra): sin tarjeta, sin comandos y el tope de
    vueltas sigue escalando como siempre (la salida del kernel es solo del motor activo)."""
    _a, b, pagos = negocios
    await _banderas(b, "v3", False)
    try:
        ctx = await _cliente(b, pagos)
        resultado, guion = await _turno(ctx, monkeypatch, "Quiero el premium", [], por_defecto=("consultar_inventario", {}))
    finally:
        await _banderas(b, "v3", True)
    assert resultado.escalated
    assert len(guion.tools) == 10 and len(set(map(tuple, guion.tools))) == 1
    assert all("Estado de la conversación" not in s for s in guion.system)
    assert len(set(guion.system)) <= 2  # el mismo prompt en cada vuelta (salvo el minuto de la hora)


