"""HU-AOS-07 · procedimientos de comercio contra el loop real, sin LLM.

Modelo guionado (`resolve_provider`), loop real (`process_conversation`, por el runner de journeys:
guarda el mensaje del cliente y la respuesta como el consumidor y el envío), tools reales de
pack-commerce, pagos de pack-payments con la pasarela simulada de los journeys y la base de
prueba. Gherkin de la HU:

- **Pedido con entrega:** a domicilio, sin dirección (o sin confirmarla) la tarea no pasa a
  confirmar.
Más: bandera apagada. Los recorridos de plan (elegir, cotizar, inscribir, Free sin link, datos del negocio
vendedor) salieron de commerce con SPEC-43/HU-COM-17: ahora viven en `pack_subscriptions.contratar_plan` y los
cubre `test_procedimiento_contratar_plan`.

Cierre por pago (decisión del dueño 2026-10-11): con pago en línea la tarea espera en
`esperar_pago` hasta `order.paid` (webhook simulado real de pack-payments → outbox → despachador →
commerce marca el pedido pagado → `order.paid` → cierre), con pago rechazado, link vencido,
"ya pagué", dato bloqueado, aislamiento entre negocios y vencimiento de la espera.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
CHECKOUT = {
    "domicilio": ["full_name", "id_document", "address", "city", "phone"],
    "recoger_en_tienda": ["full_name", "phone"],
}


CATALOGO = {
    "pack": "pack_commerce",
    "category_paths": [["Comida"]],
    "products": [
        {"name": "Hamburguesa", "description": "Doble carne.", "category_path": ["Comida"],
         "variants": [{"sku": "HAM-1", "price": 25000, "stock": 500, "attributes": {}}]},
    ],
}


# ---------------------------------------------------------------- negocio y entorno


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
    slug = f"aos07-{uuid.uuid4().hex[:8]}"
    tid = await journeys.preparar_negocio(
        {
            "nombre": "Comercio AOS07",
            "packs": ["pack_commerce", "pack_payments"],
            "plan": "premium",
            "config": {
                "pack_payments": {"public_key": "pub_test_aos07", "private_key": "prv_test_aos07",
                                  "events_secret": "test_events_aos07", "sandbox": True},
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


async def _cliente(tid: uuid.UUID, pagos: Any) -> Any:
    customer_id, conversation_id = await journeys.nuevo_cliente(tid, "Ana")
    return journeys.Contexto(tid, customer_id, conversation_id, pagos)


class _Guion:
    """Modelo guionado: un paso por llamada, texto o `(tool, args)`. Guarda las tools que vio en
    cada llamada y el resultado de la tool anterior."""

    provider_name = "fake"
    model = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Any]) -> None:
        self.pasos = list(pasos)
        self.tools: list[list[str]] = []
        self.resultados: list[str] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMToolCall

        self.tools.append(sorted(t.name for t in kwargs.get("tools") or []))
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


async def _tarea(ctx: Any) -> Any:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(ctx.tenant_id):
        async with get_session() as s:
            return (await s.execute(text(
                "select id, procedure_id, stage, status, slots, pending_confirmation from conversation_tasks "
                "where tenant_id = :t and conversation_id = :c order by created_at desc limit 1"
            ), {"t": ctx.tenant_id, "c": ctx.conversation_id})).first()


def _valor(tarea: Any, dato: str) -> Any:
    return (tarea.slots.get(dato) or {}).get("value")


async def _filas(ctx: Any, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(ctx.tenant_id):
        async with get_session() as s:
            return list((await s.execute(text(sql), {"t": ctx.tenant_id, **params})).all())


async def _auditoria(ctx: Any, accion: str) -> list[Any]:
    return await _filas(
        ctx,
        "select action, output from audit_log where tenant_id = :t and conversation_id = :c and actor = :a "
        "order by created_at",
        c=ctx.conversation_id, a=f"tool:{accion}",
    )


# ---------------------------------------------------------------- Gherkin


async def test_pedido_a_domicilio_no_pasa_a_confirmar_sin_la_direccion_confirmada(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid, pagos = negocio
    ctx = await _cliente(tid, pagos)
    _, guion = await _turno(ctx, monkeypatch, "Quiero dos hamburguesas a domicilio, pago por transferencia", [
        ("iniciar_procedimiento", {"procedure_id": PEDIDO}),
        ("generar_cotizacion", {"items": [{"variant_id": "HAM-1", "quantity": 2}]}),
        ("actualizar_tarea", {"slots": {"delivery_method": "domicilio", "payment_method": "transferencia"}}),
        ("confirmar_cotizacion", {}),
        "¿A qué dirección te lo envío?",
    ])
    assert "no corresponde a la etapa" in guion.resultados[-1]
    assert (await _tarea(ctx)).stage == "entrega"

    resultado, guion = await _turno(ctx, monkeypatch, "Calle 1 # 2-3", [
        ("actualizar_tarea", {"slots": {"address": "Calle 1 # 2-3"}}),
        ("confirmar_cotizacion", {}),
        "¿Confirmas la dirección?",
    ])
    assert "no corresponde a la etapa" in guion.resultados[-1]  # dirección sin confirmar
    assert (await _tarea(ctx)).stage == "entrega"
    assert resultado.confirmacion is not None and "Calle 1 # 2-3" in resultado.text

    resultado, _ = await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        "Te muestro el total.",
    ])
    assert (await _tarea(ctx)).stage == "confirmar"
    assert "Hamburguesa: 2" in resultado.text and "Total: $50.000" in resultado.text

    await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        ("crear_pedido", {"payment_method": "transferencia", "delivery_method": "domicilio",
                          "customer_data": {"full_name": "Ana", "id_document": "1", "city": "Bogotá", "phone": "3001"}}),
        "Listo.",
    ])
    tarea = await _tarea(ctx)
    assert tarea.status == "completed"
    pedido = await _filas(ctx, "select delivery_address, total from pack_commerce_orders where tenant_id = :t "
                               "and customer_id = :c", c=ctx.customer_id)
    assert [(f.delivery_address, float(f.total)) for f in pedido] == [("Calle 1 # 2-3", 50000)]


async def test_una_direccion_sin_confirmar_atascada_se_desatasca(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """El caso de test del 2026-10-12 (adenda de ADR-0113), ahora con la dirección de `tomar_pedido`
    (desde `contratar_plan` 1.2.0 el correo se confirma en el resumen único): con el código de antes
    la etapa queda con todo, sin confirmar y sin nada pendiente; el turno siguiente lo pide con el
    mensaje del kernel y un "sí" a ese mensaje lo confirma."""
    from core.kernel.procedures.ejecucion import ContextoMotor

    tid, pagos = negocio
    ctx = await _cliente(tid, pagos)

    async def _nada(*_: Any, **__: Any) -> None:
        return None

    with monkeypatch.context() as antes:
        antes.setattr(ContextoMotor, "_pedir_dato_debido", _nada)
        antes.setattr(ContextoMotor, "al_empezar_turno", _nada)
        await _turno(ctx, monkeypatch, "Quiero dos hamburguesas a domicilio, pago por transferencia", [
            ("iniciar_procedimiento", {"procedure_id": PEDIDO}),
            ("generar_cotizacion", {"items": [{"variant_id": "HAM-1", "quantity": 2}]}),
            ("actualizar_tarea", {"slots": {"delivery_method": "domicilio", "payment_method": "transferencia"}}),
            "¿A qué dirección te lo envío?",
        ])
        resultado, _ = await _turno(ctx, monkeypatch, "Calle 1 # 2-3", [
            ("actualizar_tarea", {"slots": {"address": "Calle 1 # 2-3"}}),
            "Anotado.",
        ])
        assert resultado.confirmacion is None
    tarea = await _tarea(ctx)
    assert tarea.stage == "entrega" and tarea.pending_confirmation is None

    resultado, _ = await _turno(ctx, monkeypatch, "¿y entonces?", ["Estoy revisando."])
    assert resultado.confirmacion is not None and "• Dirección: Calle 1 # 2-3" in resultado.text
    assert "responde «sí»" in resultado.text

    await _turno(ctx, monkeypatch, "sí", [("confirmar_cotizacion", {}), "Te muestro el total."])
    tarea = await _tarea(ctx)
    assert tarea.stage == "confirmar" and (tarea.slots.get("address") or {}).get("confirmed") is True


async def test_con_la_bandera_apagada_no_se_ofrece_nada_nuevo(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid, pagos = negocio
    await _banderas(tid, False)
    try:
        ctx = await _cliente(tid, pagos)
        _, guion = await _turno(ctx, monkeypatch, "¿Ya pagué?", [
            ("verificar_pago_pedido", {}),  # inventada o inyectada: no se ejecuta
            "Claro, te cuento.",
        ])
    finally:
        await _banderas(tid, True)
    assert "no está disponible" in guion.resultados[0]
    assert await _filas(ctx, "select id from pack_commerce_quotes where tenant_id = :t and customer_id = :c",
                        c=ctx.customer_id) == []
    ofrecidas = set(guion.tools[0])
    assert "generar_cotizacion" in ofrecidas and "crear_pedido" in ofrecidas
    assert "verificar_pago_pedido" not in ofrecidas
    assert not ofrecidas & {"iniciar_procedimiento", "actualizar_tarea", "confirmar_tarea", "cancelar_tarea"}
    assert await _tarea(ctx) is None


async def test_las_frases_de_los_procedimientos_se_verifican_solo_con_el_motor(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """"Quedó registrado tu pedido" solo lo declara `tomar_pedido` (las `COMPLETION_CLAIMS` de
    commerce no lo atrapan): con el motor activo, sin `crear_pedido`, el verificador lo rechaza."""
    tid, pagos = negocio
    afirma, corrige = "Listo, quedó registrado tu pedido.", "Te ayudo a terminar tu compra."
    ctx = await _cliente(tid, pagos)
    resultado, _ = await _turno(ctx, monkeypatch, "¿Ya está?", [afirma, corrige])
    assert resultado.text == corrige
    await _banderas(tid, False)
    try:
        ctx = await _cliente(tid, pagos)
        resultado, _ = await _turno(ctx, monkeypatch, "¿Ya está?", [afirma, corrige])
    finally:
        await _banderas(tid, True)
    assert resultado.text == afirma


# ---------------------------------------------------------------- cierre por pago (dueño, 2026-10-11)


async def _hasta_el_link(ctx: Any, monkeypatch: Any) -> Any:
    """Seis hamburguesas para recoger con pago en línea hasta mandar el link: la tarea queda esperando el pago."""
    await _turno(ctx, monkeypatch, "Seis hamburguesas para recoger, pago en línea", [
        ("iniciar_procedimiento", {"procedure_id": PEDIDO}),
        ("generar_cotizacion", {"items": [{"variant_id": "HAM-1", "quantity": 6}]}),
        ("actualizar_tarea", {"slots": {"delivery_method": "recoger_en_tienda", "payment_method": "pago_en_linea"}}),
        ("confirmar_cotizacion", {}),
        "Te muestro el total.",
    ])
    await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        ("crear_pedido", {"payment_method": "pago_en_linea", "delivery_method": "recoger_en_tienda",
                          "customer_data": {"full_name": "Ana", "phone": "3001"}}),
        ("confirmar_pago_pedido", {}),
        "Listo, ya puedes pagar.",
    ])
    tarea = await _tarea(ctx)
    assert (tarea.stage, tarea.status) == ("esperar_pago", "active")
    [pedido] = await _filas(ctx, "select id, public_order_number, payment_status, total from pack_commerce_orders "
                                 "where tenant_id = :t and customer_id = :c", c=ctx.customer_id)
    assert _valor(tarea, "order_id") == str(pedido.id)
    return pedido


async def _webhook(tid: uuid.UUID, reference: str, estado: str, monto: float, provider_reference: str | None = None) -> None:
    """El webhook verificado de la pasarela, por el camino real de pack-payments (transacción y outbox)."""
    from pack_payments.module import _apply_webhook_event
    from pack_payments.providers.base import WebhookEvent

    from core.shared.tenant import tenant_scope

    evento = WebhookEvent(reference=reference, provider_reference=provider_reference or f"sim-{reference}",
                          status=estado, raw={"simulado": True}, amount=monto)
    async with tenant_scope(tid):
        assert await _apply_webhook_event(evento, {"simulado": True}, "simulado") is not None


async def _despachar(tid: uuid.UUID) -> None:
    """Entrega el outbox del negocio como el worker (`entregar_evento`), en varias rondas."""
    ctx = journeys.Contexto(tid, uuid.uuid4(), uuid.uuid4(), None)
    await journeys.paso_outbox(ctx, {"outbox": True, "rondas": 5}, [])


async def _eventos(ctx: Any, topic: str) -> list[Any]:
    return await _filas(ctx, "select payload, status from domain_events where tenant_id = :t and topic = :p",
                        p=topic)


async def _mi_order_paid(ctx: Any, order_id: Any) -> list[Any]:
    return [e for e in await _eventos(ctx, "order.paid") if e.payload.get("order_id") == str(order_id)]


async def test_el_pago_confirmado_por_el_webhook_cierra_la_tarea(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset())
    ctx = await _cliente(tid, pagos)
    pedido = await _hasta_el_link(ctx, monkeypatch)

    # Mientras espera: solo las acciones de la espera (más las del core y los comandos); el pedido ya
    # existe, así que el método de pago no se puede cambiar (sería un segundo pedido).
    _, guion = await _turno(ctx, monkeypatch, "¿Ya quedó mi pago? Mejor lo pago por transferencia", [
        ("verificar_pago_pedido", {}),
        ("actualizar_tarea", {"slots": {"payment_method": "transferencia"}}),
        ("crear_pedido", {"payment_method": "transferencia", "customer_data": {}}),
        "Todavía no veo tu pago.",
    ])
    ofrecidas = set(guion.tools[0])
    assert {"verificar_pago_pedido", "confirmar_pago_pedido", "consultar_inventario"} <= ofrecidas
    assert not ofrecidas & {"crear_pedido", "confirmar_cotizacion", "generar_cotizacion"}
    assert "pago_pendiente" in guion.resultados[0]
    assert "ya no se puede cambiar" in guion.resultados[1]
    assert "no corresponde a la etapa" in guion.resultados[2]
    tarea = await _tarea(ctx)
    assert (tarea.stage, tarea.status, _valor(tarea, "payment_method")) == ("esperar_pago", "active", "pago_en_linea")

    await _webhook(tid, pedido.public_order_number, "aprobado", float(pedido.total))
    await _despachar(tid)
    tarea = await _tarea(ctx)
    assert (tarea.status, tarea.stage) == ("completed", "esperar_pago")
    cierre = await _filas(ctx, "select data from task_events where tenant_id = :t and task_id = :k and type = 'completed'",
                          k=tarea.id)
    assert [c.data.get("event") for c in cierre] == ["pack_commerce.order.paid"]
    [estado] = await _filas(ctx, "select payment_status from pack_commerce_orders where tenant_id = :t and id = :o",
                            o=pedido.id)
    assert estado.payment_status == "pagado"
    assert [e.status for e in await _mi_order_paid(ctx, pedido.id)] == ["entregado"]

    # Un webhook repetido no emite otro `order.paid` ni toca la tarea cerrada.
    await _webhook(tid, pedido.public_order_number, "aprobado", float(pedido.total))
    await _despachar(tid)
    assert len(await _mi_order_paid(ctx, pedido.id)) == 1


async def test_pago_rechazado_sigue_esperando_y_el_reintento_aprobado_cierra(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset())
    ctx = await _cliente(tid, pagos)
    pedido = await _hasta_el_link(ctx, monkeypatch)

    await _webhook(tid, pedido.public_order_number, "rechazado", float(pedido.total))
    await _despachar(tid)
    tarea = await _tarea(ctx)
    assert (tarea.stage, tarea.status) == ("esperar_pago", "active")  # el cliente puede reintentar
    assert await _mi_order_paid(ctx, pedido.id) == []  # un rechazo no emite nada
    _, guion = await _turno(ctx, monkeypatch, "Ya pagué", [("verificar_pago_pedido", {}), "Tu pago fue rechazado."])
    assert "pago_rechazado" in guion.resultados[0]
    assert (await _tarea(ctx)).status == "active"

    await _webhook(tid, pedido.public_order_number, "aprobado", float(pedido.total))  # reintento en el mismo link
    await _despachar(tid)
    tarea = await _tarea(ctx)
    assert tarea.status == "completed"


async def test_link_vencido_sale_uno_nuevo_y_el_pago_en_el_nuevo_cierra(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import pack_payments.payments as capacidad
    from pack_payments.providers.base import PaymentLinkResult

    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset())
    ctx = await _cliente(tid, pagos)
    pedido = await _hasta_el_link(ctx, monkeypatch)
    viejo = _valor(await _tarea(ctx), "link")
    await _filas(ctx, "update pack_payments_transactions set created_at = now() - interval '25 hours' "
                      "where tenant_id = :t and reference = :r returning id", r=pedido.public_order_number)

    original = capacidad.build_provider

    def segundo_link(config: Any) -> Any:  # la pasarela da otro link y otra referencia propia
        proveedor = original(config)
        crear = proveedor.create_payment_link

        async def create_payment_link(**kwargs: Any) -> Any:
            r = await crear(**kwargs)
            return PaymentLinkResult(provider_reference=f"{r.provider_reference}-2", payment_link_url=f"{r.payment_link_url}?n=2")

        proveedor.create_payment_link = create_payment_link
        return proveedor

    monkeypatch.setattr(capacidad, "build_provider", segundo_link)
    await _turno(ctx, monkeypatch, "El link ya no me abre", [("confirmar_pago_pedido", {}), "Te mando uno nuevo."])
    tarea = await _tarea(ctx)
    nuevo = _valor(tarea, "link")
    assert nuevo == f"{viejo}?n=2" and tarea.stage == "esperar_pago"  # el resultado de la acción sí se anota
    transacciones = await _filas(ctx, "select status, deleted_at from pack_payments_transactions where tenant_id = :t "
                                      "and reference = :r order by created_at", r=pedido.public_order_number)
    assert [(t.status, t.deleted_at is not None) for t in transacciones] == [("expirado", True), ("pendiente", False)]

    await _webhook(tid, pedido.public_order_number, "aprobado", float(pedido.total), provider_reference=f"sim-{pedido.public_order_number}-2")
    await _despachar(tid)
    assert (await _tarea(ctx)).status == "completed"


async def test_ya_pague_con_el_pedido_pagado_cierra_aunque_el_evento_no_haya_llegado(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset())
    ctx = await _cliente(tid, pagos)
    pedido = await _hasta_el_link(ctx, monkeypatch)
    await _filas(ctx, "update pack_commerce_orders set payment_status = 'pagado' where tenant_id = :t and id = :o "
                      "returning id", o=pedido.id)
    _, guion = await _turno(ctx, monkeypatch, "Ya pagué", [("verificar_pago_pedido", {}), "¡Recibimos tu pago!"])
    assert "pago_confirmado" in guion.resultados[0]
    tarea = await _tarea(ctx)
    assert tarea.status == "completed" and _valor(tarea, "pago_confirmado") == "confirmado"


async def test_un_pago_de_otro_negocio_o_de_otro_pedido_no_cierra_la_tarea(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.events.outbox import emit_domain
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset())
    ctx = await _cliente(tid, pagos)
    pedido = await _hasta_el_link(ctx, monkeypatch)
    slug_b = f"aos07b-{uuid.uuid4().hex[:8]}"
    tid_b = await journeys.preparar_negocio(
        {"nombre": "Otro comercio", "packs": ["pack_commerce", "pack_payments"], "plan": "premium",
         "config": {"pack_payments": {"public_key": "pub_test_b", "private_key": "prv_test_b",
                                      "events_secret": "test_events_b", "sandbox": True}},
         "catalogo": CATALOGO},
        pagos, slug=slug_b,
    )
    try:
        await _banderas(tid_b, True)
        # En el otro negocio: un pago con el MISMO número público y un `order.paid` con el id del
        # pedido de este negocio (lo que llegaría si alguien lo inyectara allá).
        async with tenant_scope(tid_b):
            from core.db.session import get_session

            async with get_session() as s:
                await emit_domain(s, tenant_id=tid_b, topic="payment.confirmed", dedupe_key=f"t:{uuid.uuid4()}",
                                  payload={"reference": pedido.public_order_number, "provider": "simulado",
                                           "provider_reference": "sim-x", "amount": float(pedido.total)})
                await emit_domain(s, tenant_id=tid_b, topic="order.paid", dedupe_key=f"t:{uuid.uuid4()}",
                                  payload={"order_id": str(pedido.id), "public_order_number": pedido.public_order_number})
        await _despachar(tid_b)
        # En este negocio: el `order.paid` de OTRO pedido.
        async with tenant_scope(tid):
            async with get_session() as s:
                await emit_domain(s, tenant_id=tid, topic="order.paid", dedupe_key=f"t:{uuid.uuid4()}",
                                  payload={"order_id": str(uuid.uuid4()), "public_order_number": "OC-9999"})
        await _despachar(tid)
        tarea = await _tarea(ctx)
        assert (tarea.stage, tarea.status) == ("esperar_pago", "active")
        [estado] = await _filas(ctx, "select payment_status from pack_commerce_orders where tenant_id = :t and id = :o",
                                o=pedido.id)
        assert estado.payment_status == "pendiente"
    finally:
        await deactivate_tenant(str(tid_b))
        await purge_tenant(str(tid_b), f"ELIMINAR {slug_b}")
    await _webhook(tid, pedido.public_order_number, "aprobado", float(pedido.total))
    await _despachar(tid)
    assert (await _tarea(ctx)).status == "completed"


async def test_una_espera_sin_pago_vence_y_no_toca_las_recientes(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.kernel.procedures.esperas import MOTIVO, vencer_esperas_del_negocio

    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset())
    vieja, reciente = await _cliente(tid, pagos), await _cliente(tid, pagos)
    await _hasta_el_link(vieja, monkeypatch)
    await _hasta_el_link(reciente, monkeypatch)
    tarea_vieja = await _tarea(vieja)
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("update conversation_tasks set updated_at = now() - interval '73 hours' "
                                    "where tenant_id = :t and id = :k"), {"t": tid, "k": tarea_vieja.id})
    finally:
        await engine.dispose()
    assert await vencer_esperas_del_negocio(tid) == 1
    cerrada = await _filas(vieja, "select status, status_reason from conversation_tasks where tenant_id = :t and id = :k",
                           k=tarea_vieja.id)
    assert [(c.status, c.status_reason) for c in cerrada] == [("cancelled", MOTIVO)]
    assert (await _tarea(reciente)).status == "active"
    assert await vencer_esperas_del_negocio(tid) == 0  # idempotente


async def test_pedido_para_recoger_pagado_en_linea_tambien_espera_el_pago(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid, pagos = negocio
    ctx = await _cliente(tid, pagos)
    await _turno(ctx, monkeypatch, "Seis hamburguesas para recoger, pago en línea", [
        ("iniciar_procedimiento", {"procedure_id": PEDIDO}),
        ("generar_cotizacion", {"items": [{"variant_id": "HAM-1", "quantity": 6}]}),
        ("actualizar_tarea", {"slots": {"delivery_method": "recoger_en_tienda", "payment_method": "pago_en_linea"}}),
        ("confirmar_cotizacion", {}),
        "Te muestro el total.",
    ])
    assert (await _tarea(ctx)).stage == "confirmar"
    await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        ("crear_pedido", {"payment_method": "pago_en_linea", "delivery_method": "recoger_en_tienda",
                          "customer_data": {"full_name": "Ana", "phone": "3001"}}),
        ("confirmar_pago_pedido", {}),
        "Listo, ya puedes pagar.",
    ])
    tarea = await _tarea(ctx)
    assert (tarea.procedure_id, tarea.stage, tarea.status) == (PEDIDO, "esperar_pago", "active")
    [pedido] = await _filas(ctx, "select public_order_number, total from pack_commerce_orders where tenant_id = :t "
                                 "and customer_id = :c", c=ctx.customer_id)
    await _webhook(tid, pedido.public_order_number, "aprobado", float(pedido.total))
    await _despachar(tid)
    assert (await _tarea(ctx)).status == "completed"


# ---------------------------------------------------------------- «Anoté tu dirección» (pass^4, 2026-10-18)
ANOTE = "Anoté tu dirección: Calle 10 # 20-30, Bogotá. ¿Es correcta?"


async def _pedido_en_entrega(negocio: Any, monkeypatch: Any) -> Any:
    tid, pagos = negocio
    ctx = await _cliente(tid, pagos)
    await _turno(ctx, monkeypatch, "Quiero dos hamburguesas a domicilio, pago por transferencia", [
        ("iniciar_procedimiento", {"procedure_id": PEDIDO}),
        ("generar_cotizacion", {"items": [{"variant_id": "HAM-1", "quantity": 2}]}),
        ("actualizar_tarea", {"slots": {"delivery_method": "domicilio", "payment_method": "transferencia"}}),
        "¿A qué dirección te lo envío?",
    ])
    assert (await _tarea(ctx)).stage == "entrega"
    return ctx


async def test_anote_tu_direccion_con_el_dato_fijado_por_el_motor_no_escala(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """v3: el modelo fija la dirección con `actualizar_tarea` y dice «Anoté tu dirección». El
    comando aceptado del turno respalda la afirmación (antes escalaba por `dato_de_contacto_guardado`)."""
    ctx = await _pedido_en_entrega(negocio, monkeypatch)
    resultado, _ = await _turno(ctx, monkeypatch, "Calle 10 # 20-30, Bogotá", [
        ("actualizar_tarea", {"slots": {"address": "Calle 10 # 20-30, Bogotá"}}),
        # El modelo insiste igual en el reintento: sin evidencia, el turno escalaría.
        ANOTE, ANOTE,
    ])
    assert resultado is not None and not resultado.escalated, resultado
    # El motor pide confirmar la dirección (dato `confirm`): el turno sigue, no hay relevo.
    assert "Calle 10 # 20-30, Bogotá" in resultado.text
    assert ((await _tarea(ctx)).slots.get("address") or {}).get("value") == "Calle 10 # 20-30, Bogotá"


async def test_anote_tu_direccion_sin_fijarla_sigue_escalando(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Sin el comando (o con uno rechazado) no hay evidencia: el verificador reintenta y escala."""
    ctx = await _pedido_en_entrega(negocio, monkeypatch)
    resultado, _ = await _turno(ctx, monkeypatch, "Calle 10 # 20-30, Bogotá", [ANOTE, ANOTE])
    assert resultado is not None and resultado.escalated
    motivo = await journeys.motivo_del_relevo(ctx, datetime.now(UTC) - timedelta(minutes=1))
    assert "dato_de_contacto_guardado" in motivo, motivo

    ctx = await _pedido_en_entrega(negocio, monkeypatch)
    resultado, _ = await _turno(ctx, monkeypatch, "Calle 10 # 20-30, Bogotá", [
        # Un dato que el procedimiento no tiene: el comando se rechaza entero (todo o nada).
        ("actualizar_tarea", {"slots": {"address": "Calle 10 # 20-30, Bogotá", "apodo": "Anita"}}),
        ANOTE, ANOTE,
    ])
    assert resultado is not None and resultado.escalated
