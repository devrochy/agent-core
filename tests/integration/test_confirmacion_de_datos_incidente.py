"""Confirmaciones de datos visibles y "sí" a secas para datos (adenda 2026-10-12 de ADR-0113).

Reproduce, con el loop real y un modelo guionado (sin LLM), la prueba en test del 2026-10-12
(23:45–23:52 UTC, `pack_commerce.contratar_plan` v1.1.0, canal WhatsApp): el cliente dio el negocio,
el tipo y el correo al elegir el plan; la confirmación del correo se pidió en `elegir_plan`, el cambio
de etapa la venció, nadie la volvió a pedir y la tarea quedó atascada en `datos` (el modelo terminó
diciendo "no tengo la herramienta para generar el pedido"). Además el modelo fijó `full_name` con el
nombre del perfil del canal ("Rob") sin que el cliente lo escribiera.

- `test_el_caso_del_incidente_llega_a_pagar_con_link`: la misma secuencia, con el arreglo, llega a
  confirmar → pagar con link (etapa `esperar_pago`).

Desde `contratar_plan` 1.2.0 (adenda 2026-10-13, decisión del dueño) el correo se confirma dentro
del resumen único y no con una pregunta aparte: el estado atascado de test ya no puede darse con
este procedimiento. El mecanismo de confirmar un dato que exige la salida de una etapa sigue para
los demás (la dirección de `tomar_pedido`): lo cubre
`test_procedimientos_de_comercio.py::test_una_direccion_sin_confirmar_atascada_se_desatasca`.
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

PLAN = "pack_commerce.contratar_plan"
# Como el negocio `solvershub` en test: el plan pide los datos de la persona y los del negocio.
CHECKOUT = {
    "domicilio": ["full_name", "id_document", "address", "city", "phone"],
    "plan": ["full_name", "id_document", "phone", "email", "business_name", "vertical"],
}
CATALOGO = {
    "pack": "pack_commerce",
    "category_paths": [["Planes"]],
    "products": [
        {"name": "Plan Premium", "description": "Todo incluido.", "category_path": ["Planes"],
         "variants": [{"sku": "PREMIUM-MES", "price": 600000, "stock": 0,
                       "attributes": {"periodicidad": "mensual", "trial_dias": 0}}]},
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


@pytest.fixture(scope="module")
async def negocio() -> AsyncIterator[tuple[uuid.UUID, Any]]:
    import pack_payments.payments as capacidad

    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    original = capacidad.build_provider
    await journeys._preparar_proceso()
    pagos = journeys.PagoSimulado(monto_minimo=150000)
    slug = f"confdatos-{uuid.uuid4().hex[:8]}"
    tid = await journeys.preparar_negocio(
        {
            "nombre": "Vendedor de planes",
            "packs": ["pack_commerce", "pack_payments"],
            "plan": "premium",
            "config": {
                "pack_payments": {"public_key": "pub_test_confdatos", "private_key": "prv_test_confdatos",
                                  "events_secret": "test_events_confdatos", "sandbox": True},
            },
            "catalogo": CATALOGO,
        },
        pagos,
        slug=slug,
    )
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_commerce", {"checkout_fields": CHECKOUT})
    await _banderas(tid, True)
    yield tid, pagos
    capacidad.build_provider = original
    await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


class _Guion:
    """Modelo guionado: un paso por llamada, texto o `(tool, args)`. Guarda las tools ofrecidas,
    el prompt de sistema y el resultado de la tool anterior de cada llamada."""

    provider_name = "fake"
    model = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Any]) -> None:
        self.pasos = list(pasos)
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
    # El nombre del perfil del canal es "Rob" (en test era el del perfil de WhatsApp).
    customer_id, conversation_id = await journeys.nuevo_cliente(tid, "Rob")
    return journeys.Contexto(tid, customer_id, conversation_id, pagos)


async def _tarea(ctx: Any) -> Any:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(ctx.tenant_id):
        async with get_session() as s:
            return (await s.execute(text(
                "select id, stage, status, slots, pending_confirmation from conversation_tasks "
                "where tenant_id = :t and conversation_id = :c order by created_at desc limit 1"
            ), {"t": ctx.tenant_id, "c": ctx.conversation_id})).first()


async def _eventos(ctx: Any, task_id: uuid.UUID) -> list[tuple[str, Any]]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(ctx.tenant_id):
        async with get_session() as s:
            filas = (await s.execute(text(
                "select type, data from task_events where tenant_id = :t and task_id = :k order by seq"
            ), {"t": ctx.tenant_id, "k": task_id})).all()
    return [(f.type, f.data) for f in filas]


def _valor(tarea: Any, dato: str) -> Any:
    return (tarea.slots.get(dato) or {}).get("value")


def _confirmado(tarea: Any, dato: str) -> bool:
    return (tarea.slots.get(dato) or {}).get("confirmed") is True


async def _hasta_los_datos(ctx: Any, monkeypatch: Any, vertical: str) -> None:
    """23:45:21 → 23:48:04 del incidente, con el mismo guion del modelo."""
    datos = journeys.datos_unicos(ctx.customer_id)
    resultado, _ = await _turno(
        ctx, monkeypatch,
        f"Hola, quiero el plan premium mensual para mi negocio Pizzería Roma, es un {vertical}, mi correo es contacto@pizzeriaroma.co",
        [
            ("iniciar_procedimiento", {"procedure_id": PLAN}),
            ("consultar_inventario", {}),
            ("actualizar_tarea", {"slots": {
                "business_name": "Pizzería Roma", "email": "contacto@pizzeriaroma.co", "variant_id": "PREMIUM-MES",
                "vertical": vertical,
            }}),
            ("cotizar_plan", {}),
            "Te resumo: negocio Pizzería Roma, correo contacto@pizzeriaroma.co, plan Premium mensual. ¿Está todo bien así?",
        ],
    )
    tarea = await _tarea(ctx)
    assert tarea.stage == "datos" and _valor(tarea, "email") == "contacto@pizzeriaroma.co"
    # El correo todavía no se pide: no es lo único que le falta a `datos`. Nada queda pendiente.
    assert tarea.pending_confirmation is None and resultado.confirmacion is None

    await _turno(ctx, monkeypatch, "Si", ["Perfecto. ¿Me compartes tu número de documento y tu teléfono?"])

    # 23:47:11: el modelo agrega el nombre del perfil del canal, que el cliente nunca escribió.
    _, guion = await _turno(ctx, monkeypatch, f"{datos['documento']}, {datos['celular']}", [
        ("actualizar_tarea", {"slots": {
            "full_name": "Rob", "id_document": datos["documento"], "phone": datos["celular"],
        }}),
        ("actualizar_tarea", {"slots": {"id_document": datos["documento"], "phone": datos["celular"]}}),
        "Gracias. ¿Cuál es tu nombre completo?",
    ])
    assert "full_name no aparece en lo que escribió el cliente" in guion.resultados[0]
    tarea = await _tarea(ctx)
    assert _valor(tarea, "full_name") is None and _valor(tarea, "id_document") == datos["documento"]
    # La tarjeta pide lo que falta, en orden y como lista (adenda 2026-10-13).
    assert "Faltan estos datos, en este orden:" in guion.system[0] and "full_name" in guion.system[0]

    await _turno(ctx, monkeypatch, "Roberto Pérez", [
        ("actualizar_tarea", {"slots": {"full_name": "Roberto Pérez"}}),
        "¿Cómo quieres pagar?",
    ])


async def test_el_caso_del_incidente_llega_a_pagar_con_link(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.presets import list_presets

    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset({tid}))
    ctx = await _cliente(tid, pagos)
    await _hasta_los_datos(ctx, monkeypatch, list_presets()[0].id)

    # 23:48:04: con el método de pago ya no falta nada en `datos` (el correo se confirma en el
    # resumen único, adenda 2026-10-13): la tarea pasa a confirmar y sale el resumen del kernel con
    # el plan, el total y los datos del cliente, incluido el correo.
    resultado, guion = await _turno(ctx, monkeypatch, "Pago en linea", [
        ("actualizar_tarea", {"slots": {"payment_method": "pago_en_linea"}}),
        ("confirmar_cotizacion", {}),
        "¿Confirmás todo?",
    ])
    tarea = await _tarea(ctx)
    assert tarea.stage == "confirmar" and tarea.pending_confirmation["kind"] == "action"
    assert resultado.confirmacion is not None and resultado.confirmacion.modo == "interactive_button"
    assert "• Correo: contacto@pizzeriaroma.co" in resultado.text and "• Total: $600.000" in resultado.text
    assert "¿Confirmás todo?" not in resultado.text
    assert "confirmation_requested" not in [t for t, d in await _eventos(ctx, tarea.id) if (d.get("target") or {}).get("kind") == "slot"]

    # Un "Si" a secas NO confirma la acción con monto (HU-AOS-09 sigue estricto).
    _, guion = await _turno(ctx, monkeypatch, "Si", [
        ("confirmar_tarea", {}),
        "Para confirmar toca «Confirmar» o escribe «confirmo».",
    ])
    assert "Falta la confirmación explícita" in guion.resultados[0]
    assert (await _tarea(ctx)).stage == "confirmar"

    # Dos turnos después la petición venció: "Sí, confirmo" no vale contra ella, pero no se atasca:
    # el modelo vuelve a llamar la acción y el kernel la pide de nuevo con su mensaje.
    resultado, guion = await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        "Te muestro el resumen.",
    ])
    assert "ya venció" in guion.resultados[0]
    assert "confirmation_required" in guion.resultados[1]
    assert resultado.confirmacion is not None and "Total: $600.000" in resultado.text

    # El sí explícito al mensaje del turno anterior confirma, y se crea el pedido con el link.
    resultado, guion = await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        ("crear_pedido", {"payment_method": "pago_en_linea", "customer_data": {}}),
        ("confirmar_pago_pedido", {}),
        "Listo, aquí tienes el link para pagar.",
    ])
    tarea = await _tarea(ctx)
    assert (tarea.stage, tarea.status) == ("esperar_pago", "active"), guion.resultados
    link = _valor(tarea, "link")
    assert link and link in resultado.text
    assert _valor(tarea, "full_name") == "Roberto Pérez"


async def test_accion_antes_de_confirmar_tarea_sigue_funcionando(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Camino feliz que la re-petición de una confirmación vencida no puede romper: en el turno
    siguiente a la petición, el modelo llama primero la acción, después `confirmar_tarea` y otra vez
    la acción: la petición del turno anterior sigue valiendo (no se vuelve a pedir)."""
    from core.presets import list_presets

    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset({tid}))
    ctx = await _cliente(tid, pagos)
    await _hasta_los_datos(ctx, monkeypatch, list_presets()[0].id)
    await _turno(ctx, monkeypatch, "Pago en linea", [
        ("actualizar_tarea", {"slots": {"payment_method": "pago_en_linea"}}),
        ("confirmar_cotizacion", {}),
        "Te muestro el resumen.",
    ])
    tarea = await _tarea(ctx)
    pedida = tarea.pending_confirmation
    assert tarea.stage == "confirmar" and pedida is not None and pedida["kind"] == "action"

    _, guion = await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_cotizacion", {}),
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        "Listo.",
    ])
    assert "confirmation_required" in guion.resultados[0]
    assert '"ok": true' in guion.resultados[1]
    tarea = await _tarea(ctx)
    assert tarea.stage == "pagar", guion.resultados
    peticiones = [d for t, d in await _eventos(ctx, tarea.id) if t == "confirmation_requested"]
    assert sum(1 for d in peticiones if d["target"]["kind"] == "action") == 1  # no se volvió a pedir


async def test_lo_que_dijo_otro_cliente_no_respalda_un_dato(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """La regla "el cliente lo escribió" lee solo los mensajes de ESTA conversación (además de la
    RLS, filtra por negocio y conversación): el nombre que escribió otro cliente no vale aquí."""
    tid, pagos = negocio
    otro = await _cliente(tid, pagos)
    await _turno(otro, monkeypatch, "Me llamo Valentina Ospina", ["Hola Valentina."])
    ctx = await _cliente(tid, pagos)
    _, guion = await _turno(ctx, monkeypatch, "Quiero el plan premium mensual", [
        ("iniciar_procedimiento", {"procedure_id": PLAN}),
        ("actualizar_tarea", {"slots": {"variant_id": "PREMIUM-MES"}}),
        ("cotizar_plan", {}),
        ("actualizar_tarea", {"slots": {"full_name": "Valentina Ospina"}}),
        "¿Cuál es tu nombre?",
    ])
    assert "full_name no aparece en lo que escribió el cliente" in guion.resultados[-1]
    assert _valor(await _tarea(ctx), "full_name") is None
