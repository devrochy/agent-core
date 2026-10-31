"""Adenda 2026-10-12 de ADR-0113 · sin foco no hay camino de siempre, contra el loop real, sin LLM.

Reproduce el hallazgo de la prueba real en test (2026-10-12): negocio con `aos_procedures`, sin
tarea en foco, el modelo intenta cerrar la venta con `confirmar_cotizacion` sin iniciar el
procedimiento. Ahora la acción no se ofrece, se niega con la pista de qué recorrido iniciar, el
modelo llama `iniciar_procedimiento(pack_commerce.contratar_plan)` y sigue el procedimiento.

Modelo guionado (`resolve_provider`), loop real (`process_conversation` por el runner de journeys),
tools reales de pack-commerce y la base de prueba. Más: un lote `iniciar` + `confirmar_cotizacion`
cae en `fuera_de_etapa`, `confirmar_pago_pedido` sigue disponible sin foco y el aislamiento entre un
negocio con la bandera y otro sin ella.
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

PEDIDO = "pack_commerce.tomar_pedido"
NEGADA = "solo se puede usar dentro de un recorrido"
CHECKOUT = {"domicilio": ["full_name", "phone", "address"], "recoger_en_tienda": ["full_name", "phone"]}
CATALOGO = {
    "pack": "pack_commerce",
    "category_paths": [["Comida"]],
    "products": [
        {"name": "Hamburguesa", "description": "Doble carne.", "category_path": ["Comida"],
         "variants": [{"sku": "HAM-1", "price": 25000, "stock": 500, "attributes": {}}]},
    ],
}


async def _banderas(tid: uuid.UUID, encendidas: bool) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("update tenants set aos_procedures = false where id = :t"), {"t": tid})
            await conn.execute(
                text("update tenants set aos_runtime = :r, aos_procedures = :p where id = :t"),
                {"t": tid, "r": "v3" if encendidas else "legacy", "p": encendidas},
            )
    finally:
        await engine.dispose()


async def _alta(pagos: Any, prefijo: str, encendidas: bool) -> tuple[uuid.UUID, str]:
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
    tid = await journeys.preparar_negocio(
        {
            "nombre": f"Comercio {prefijo}",
            "packs": ["pack_commerce", "pack_payments"],
            "plan": "premium",
            "config": {"pack_payments": {"public_key": f"pub_test_{prefijo}", "private_key": f"prv_test_{prefijo}",
                                         "events_secret": f"test_events_{prefijo}", "sandbox": True}},
            "catalogo": CATALOGO,
        },
        pagos,
        slug=slug,
    )
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_commerce", {"checkout_fields": CHECKOUT})
    await _banderas(tid, encendidas)
    return tid, slug


async def _alta_agenda(pagos: Any) -> tuple[uuid.UUID, str]:
    """C: solo agenda, con la bandera. Sus procedimientos y puertas son otros que los de A."""
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    slug = f"sinfoco-c-{uuid.uuid4().hex[:8]}"
    tid = await journeys.preparar_negocio({"nombre": "Agenda sinfoco", "packs": ["pack_calendar"], "plan": "premium"},
                                          pagos, slug=slug)
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_calendar", {
            "source": "interno", "timezone": "America/Bogota", "slot_minutes": 60, "buffer_minutes": 0,
            "event_duration_minutes": 60,
        })
    await _banderas(tid, True)
    return tid, slug


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, Any]]:
    """Dos negocios con los mismos packs: A con `aos_procedures`, B sin la bandera."""
    import pack_payments.payments as capacidad

    from core.tenants import deactivate_tenant, purge_tenant

    original = capacidad.build_provider
    await journeys._preparar_proceso()
    pagos = journeys.PagoSimulado(monto_minimo=150000)
    tid_a, slug_a = await _alta(pagos, "sinfoco-a", True)
    tid_b, slug_b = await _alta(pagos, "sinfoco-b", False)
    yield tid_a, tid_b, pagos
    capacidad.build_provider = original
    for tid, slug in ((tid_a, slug_a), (tid_b, slug_b)):
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


class _Guion:
    """Modelo guionado: un paso por llamada, texto o `(tool, args)`. Guarda las tools y el prompt
    del sistema que vio en cada llamada y el resultado de la tool anterior."""

    provider_name = "fake"
    model = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Any]) -> None:
        self.pasos = list(pasos)
        self.tools: list[list[str]] = []
        self.sistema: list[str] = []
        self.resultados: list[str] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMToolCall

        self.tools.append(sorted(t.name for t in kwargs.get("tools") or []))
        self.sistema.append("\n".join(str(b) for b in kwargs.get("system_blocks") or []))
        mensajes = kwargs.get("messages") or []
        if mensajes and mensajes[-1].role == "tool":
            self.resultados.append(str(mensajes[-1].content))
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


async def _turno(ctx: Any, monkeypatch: Any, mensaje: str, pasos: list[Any]) -> tuple[Any, _Guion]:
    guion = _Guion(pasos)
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    resultado = await journeys._turno(ctx, mensaje)
    return resultado, guion


async def _cliente(tid: uuid.UUID, pagos: Any) -> Any:
    customer_id, conversation_id = await journeys.nuevo_cliente(tid, "Ana")
    return journeys.Contexto(tid, customer_id, conversation_id, pagos)


async def _filas(ctx: Any, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(ctx.tenant_id):
        async with get_session() as s:
            return list((await s.execute(text(sql), {"t": ctx.tenant_id, "c": ctx.conversation_id, **params})).all())


async def _tareas(ctx: Any) -> list[Any]:
    return await _filas(ctx, "select procedure_id, stage, status from conversation_tasks "
                             "where tenant_id = :t and conversation_id = :c order by created_at")


def _pista() -> tuple[str, str]:
    from core.kernel.tarjeta import rotulos

    r = rotulos()
    return r["sin_foco"], r["sin_foco_acciones"].split("{")[0]


# ---------------------------------------------------------------- el caso de la prueba real


async def test_sin_foco_confirmar_se_niega_y_el_modelo_inicia_el_procedimiento(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid_a, _, pagos = negocios
    ctx = await _cliente(tid_a, pagos)
    quote_ajena = str(uuid.uuid4())
    _, guion = await _turno(ctx, monkeypatch, "Quiero comprar dos hamburguesas", [
        ("confirmar_cotizacion", {"quote_id": quote_ajena}),     # el camino de siempre: negado
        ("iniciar_procedimiento", {"procedure_id": PEDIDO}),
        ("generar_cotizacion", {"items": [{"variant_id": "HAM-1", "quantity": 2}]}),
        ("actualizar_tarea", {"slots": {"delivery_method": "domicilio", "payment_method": "transferencia"}}),
        "Perfecto. ¿A qué dirección te lo envío?",
    ])
    # Lo que vio el modelo sin foco: sin la puerta, con iniciar y las consultas; y la pista del
    # kernel en el prompt, que nombra la acción que solo funciona dentro del recorrido.
    ofrecidas = set(guion.tools[0])
    assert "confirmar_cotizacion" not in ofrecidas and "iniciar_procedimiento" in ofrecidas
    assert {"consultar_inventario", "crear_pedido", "confirmar_pago_pedido"} <= ofrecidas
    sin_foco, sin_foco_acciones = _pista()
    assert sin_foco in guion.sistema[0] and f"{sin_foco_acciones}confirmar_cotizacion" in guion.sistema[0]
    # Negada con la pista de qué recorrido iniciar; no se ejecutó.
    assert NEGADA in guion.resultados[0] and "iniciar_procedimiento" in guion.resultados[0]
    assert PEDIDO in guion.resultados[0]
    auditoria = await _filas(ctx, "select action from audit_log where tenant_id = :t and conversation_id = :c "
                                  "and actor = 'tool:confirmar_cotizacion'")
    assert [f.action for f in auditoria] == ["denied"]
    # Siguió el procedimiento: la etapa `armar` ofrece generar_cotizacion y la tarea quedó en `entrega`.
    assert "generar_cotizacion" in guion.tools[2]
    assert [(f.procedure_id, f.stage, f.status) for f in await _tareas(ctx)] == [(PEDIDO, "entrega", "active")]
    cotizaciones = await _filas(ctx, "select status from pack_commerce_quotes where tenant_id = :t "
                                     "and customer_id = :k", k=ctx.customer_id)
    assert [f.status for f in cotizaciones] == ["draft"]  # nadie la aceptó por fuera del botón
    # Con foco, la tarjeta ya no lleva la pista (lleva la tarea).
    _, guion2 = await _turno(ctx, monkeypatch, "Calle 1 # 2-3", ["¿Es correcta?"])
    assert sin_foco not in guion2.sistema[0]


async def test_iniciar_y_confirmar_en_el_mismo_lote_cae_en_la_etapa(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid_a, _, pagos = negocios
    ctx = await _cliente(tid_a, pagos)
    _, guion = await _turno(ctx, monkeypatch, "Quiero hamburguesas", [
        ("iniciar_procedimiento", {"procedure_id": PEDIDO}),
        ("confirmar_cotizacion", {"quote_id": str(uuid.uuid4())}),
        "¿Cuántas quieres?",
    ])
    assert "no corresponde a la etapa" in guion.resultados[1]
    assert [(f.procedure_id, f.stage) for f in await _tareas(ctx)] == [(PEDIDO, "armar")]


async def test_sin_foco_el_link_de_un_pedido_se_sigue_pidiendo(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`confirmar_pago_pedido` no es puerta: el cliente que vuelve después de que la tarea venció
    puede pedir otra vez el link de su pedido."""
    tid_a, _, pagos = negocios
    ctx = await _cliente(tid_a, pagos)
    _, guion = await _turno(ctx, monkeypatch, "Mándame el link de pago otra vez", [
        ("confirmar_pago_pedido", {"order_id": str(uuid.uuid4())}),
        "Déjame revisar.",
    ])
    assert "confirmar_pago_pedido" in guion.tools[0]
    assert NEGADA not in guion.resultados[0]
    assert await _tareas(ctx) == []


# ---------------------------------------------------------------- aislamiento


async def test_el_otro_negocio_sin_la_bandera_no_cambia(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid_a, tid_b, pagos = negocios
    sin_foco, _ = _pista()
    ctx_b = await _cliente(tid_b, pagos)
    _, guion_b = await _turno(ctx_b, monkeypatch, "Quiero comprar hamburguesas", [
        ("confirmar_cotizacion", {"quote_id": str(uuid.uuid4())}),
        "Te ayudo.",
    ])
    ofrecidas_b = set(guion_b.tools[0])
    assert "confirmar_cotizacion" in ofrecidas_b and "crear_pedido" in ofrecidas_b
    assert not ofrecidas_b & {"iniciar_procedimiento", "actualizar_tarea", "confirmar_tarea", "cancelar_tarea"}
    assert sin_foco not in guion_b.sistema[0]
    assert NEGADA not in guion_b.resultados[0]  # la tool corrió como siempre (la cotización no existe)
    assert await _tareas(ctx_b) == []
    negadas_b = await _filas(ctx_b, "select action from audit_log where tenant_id = :t and conversation_id = :c "
                                    "and actor = 'tool:confirmar_cotizacion' and action = 'denied'")
    assert negadas_b == []

    # Intercalado: el negocio con la bandera sigue negando en el mismo proceso.
    ctx_a = await _cliente(tid_a, pagos)
    _, guion_a = await _turno(ctx_a, monkeypatch, "Confirmo la cotización", [
        ("confirmar_cotizacion", {"quote_id": str(uuid.uuid4())}),
        "Primero armemos tu pedido.",
    ])
    assert "confirmar_cotizacion" not in guion_a.tools[0] and sin_foco in guion_a.sistema[0]
    assert NEGADA in guion_a.resultados[0]


async def test_la_pista_y_la_negacion_son_del_negocio_del_turno(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Un negocio C con otros packs (solo agenda) y la bandera: su negación y su pista nombran sus
    procedimientos y puertas, nunca los de comercio de A; y A no ve los de C. Una tarea en foco en
    otra conversación de A no cambia la decisión de esta."""
    from core.tenants import deactivate_tenant, purge_tenant

    tid_a, _, pagos = negocios
    tid_c, slug_c = await _alta_agenda(pagos)
    try:
        ctx_c = await _cliente(tid_c, pagos)
        _, guion_c = await _turno(ctx_c, monkeypatch, "Agéndame mañana a las 10", [
            ("create_calendar_event", {"title": "Cita", "start_datetime": "2030-01-07T10:00:00"}),
            ("confirmar_cotizacion", {"quote_id": str(uuid.uuid4())}),   # no es de este negocio
            "¿Qué día te sirve?",
        ])
        assert NEGADA in guion_c.resultados[0] and "pack_calendar.agendar_cita" in guion_c.resultados[0]
        assert "pack_commerce" not in guion_c.resultados[0]
        assert NEGADA not in guion_c.resultados[1]  # sin commerce, no es puerta: lo frena `_tool_denial`
        assert "create_calendar_event" not in guion_c.tools[0]
        assert "confirmar_cotizacion" not in guion_c.sistema[0] and "pack_commerce" not in guion_c.sistema[0]
        assert await _tareas(ctx_c) == []
    finally:
        await deactivate_tenant(str(tid_c))
        await purge_tenant(str(tid_c), f"ELIMINAR {slug_c}")

    # A, con una tarea en foco en otra conversación: esta conversación sigue sin foco y niega.
    con_tarea = await _cliente(tid_a, pagos)
    await _turno(con_tarea, monkeypatch, "Quiero hamburguesas", [("iniciar_procedimiento", {"procedure_id": PEDIDO}), "¿Cuántas?"])
    assert [f.stage for f in await _tareas(con_tarea)] == ["armar"]
    ctx_a = await _cliente(tid_a, pagos)
    _, guion_a = await _turno(ctx_a, monkeypatch, "Agenda y confirma", [
        ("confirmar_cotizacion", {"quote_id": str(uuid.uuid4())}),
        "Primero armemos tu pedido.",
    ])
    assert NEGADA in guion_a.resultados[0] and "pack_calendar" not in guion_a.resultados[0]
    assert "create_calendar_event" not in guion_a.sistema[0]
