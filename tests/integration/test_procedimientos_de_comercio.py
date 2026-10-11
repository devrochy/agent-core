"""HU-AOS-07 · procedimientos de comercio contra el loop real, sin LLM.

Modelo guionado (`resolve_provider`), loop real (`process_conversation`, por el runner de journeys:
guarda el mensaje del cliente y la respuesta como el consumidor y el envío), tools reales de
pack-commerce, pagos de pack-payments con la pasarela simulada de los journeys y la base de
prueba. Gherkin de la HU:

- **Contratar un plan:** el cliente compra Premium mensual: el pedido tiene la variante y el total
  del servidor y el link es el de la acción (pass^4 en vivo queda para medir con OK del dueño).
- **Pedido con entrega:** a domicilio, sin dirección (o sin confirmarla) la tarea no pasa a
  confirmar.
- **Cambio de opinión:** el cliente cambia de plan antes de confirmar: la cotización se recalcula y
  la anterior no se usa.

Más: plan pago por transferencia, Free sin link, datos del negocio vendedor (términos del alta) y
bandera apagada.
"""

from __future__ import annotations

import importlib.util
import json
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
PEDIDO = "pack_commerce.tomar_pedido"
CHECKOUT = {
    "domicilio": ["full_name", "id_document", "address", "city", "phone"],
    "recoger_en_tienda": ["full_name", "phone"],
    "plan": ["full_name", "id_document", "phone", "email"],
}


def _variante(sku: str, precio: int, periodicidad: str | None = None, stock: int = 0) -> dict[str, Any]:
    attrs = {"periodicidad": periodicidad, "trial_dias": 0} if periodicidad else {}
    return {"sku": sku, "price": precio, "stock": stock, "attributes": attrs}


CATALOGO = {
    "pack": "pack_commerce",
    "category_paths": [["Planes"], ["Comida"]],
    "products": [
        {"name": "Plan Free", "description": "Sin costo.", "category_path": ["Planes"],
         "variants": [_variante("PLAN-FREE", 0, "mensual")]},
        {"name": "Plan Premium", "description": "Todo incluido.", "category_path": ["Planes"],
         "variants": [_variante("PREMIUM-MES", 600000, "mensual"), _variante("PREMIUM-ANO", 6000000, "anual")]},
        {"name": "Hamburguesa", "description": "Doble carne.", "category_path": ["Comida"],
         "variants": [_variante("HAM-1", 25000, stock=50)]},
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
                "select id, procedure_id, stage, status, slots from conversation_tasks "
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


async def _variante_id(ctx: Any, sku: str) -> str:
    fila = (await _filas(ctx, "select id from pack_commerce_product_variants where tenant_id = :t and sku = :s",
                         s=sku))[0]
    return str(fila.id)


async def _auditoria(ctx: Any, accion: str) -> list[Any]:
    return await _filas(
        ctx,
        "select action, output from audit_log where tenant_id = :t and conversation_id = :c and actor = :a "
        "order by created_at",
        c=ctx.conversation_id, a=f"tool:{accion}",
    )


DATOS_ANA = {"full_name": "Ana Gómez", "id_document": "1012345678", "phone": "3001234567", "email": "Ana@Example.com"}


async def _hasta_el_resumen(ctx: Any, monkeypatch: Any, plan: str, pago: str) -> Any:
    """Turnos 1-3 de contratar un plan: elegir, cotizar, datos, confirmar el correo y pedir el sí."""
    resultado, guion = await _turno(ctx, monkeypatch, f"Hola, quiero contratar el plan {plan}", [
        ("iniciar_procedimiento", {"procedure_id": PLAN}),
        ("actualizar_tarea", {"slots": {"variant_id": plan}}),
        ("cotizar_plan", {}),
        "Perfecto. ¿Me compartes tu nombre, cédula, celular, correo y cómo quieres pagar?",
    ])
    assert "cotizar_plan" not in guion.tools[0] and "iniciar_procedimiento" in guion.tools[0]
    assert "cotizar_plan" in guion.tools[2]  # la etapa `cotizar` la ofrece
    tarea = await _tarea(ctx)
    assert (tarea.procedure_id, tarea.stage) == (PLAN, "datos")
    assert _valor(tarea, "requiere_pago") is (plan != "free")

    resultado, _ = await _turno(ctx, monkeypatch, "Ana Gómez, 1012345678, 3001234567, Ana@Example.com", [
        ("actualizar_tarea", {"slots": {**DATOS_ANA, **({"payment_method": pago} if pago else {})}}),
        "¿Me confirmas tu correo?",
    ])
    assert resultado.confirmacion is not None and "ana@example.com" in resultado.text

    resultado, guion = await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        "Te muestro el resumen para que lo confirmes.",
    ])
    tarea = await _tarea(ctx)
    assert tarea.stage == "confirmar"
    assert "aplicar_promocion" not in guion.tools[1]  # en confirmar nada cambia el total
    return resultado


# ---------------------------------------------------------------- Gherkin


async def test_contratar_premium_mensual_pedido_y_link_del_servidor(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset())
    ctx = await _cliente(tid, pagos)
    resultado = await _hasta_el_resumen(ctx, monkeypatch, "premium mensual", "pago_en_linea")
    # El cliente confirma lo que leyó el servidor de la cotización, no lo que redactó el modelo.
    assert "Total: $600.000" in resultado.text and "Plan: Plan Premium" in resultado.text
    assert "Periodicidad: mensual" in resultado.text and "resumen para que" not in resultado.text

    resultado, guion = await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        ("confirmar_pago_pedido", {"order_id": str(uuid.uuid4())}),  # un pedido que no es el de la tarea
        ("crear_pedido", {"payment_method": "pago_en_linea", "customer_data": {"full_name": "Ana Gómez"}}),
        ("confirmar_pago_pedido", {}),
        "Listo, ya puedes pagar.",
    ])
    assert "no coincide con el dato vigente" in guion.resultados[2]
    tarea = await _tarea(ctx)
    assert tarea.status == "completed", guion.resultados
    premium = await _variante_id(ctx, "PREMIUM-MES")
    pedido = (await _filas(ctx, "select id, total, payment_status, public_order_number, payment_link_url "
                                "from pack_commerce_orders where tenant_id = :t and customer_id = :c",
                           c=ctx.customer_id))
    assert len(pedido) == 1 and float(pedido[0].total) == 600000 and pedido[0].payment_status == "pendiente"
    lineas = await _filas(ctx, "select variant_id from pack_commerce_order_items where tenant_id = :t and order_id = :o",
                          o=pedido[0].id)
    assert [str(f.variant_id) for f in lineas] == [premium]
    # Los datos del cliente del pedido salen de la tarea (ligados a `customer_data`).
    entrada = (await _auditoria(ctx, "crear_pedido"))[-1]
    assert entrada.action == "execute" and entrada.output["status"] == "confirmed"
    link = f"https://pagos.simulados/{pedido[0].public_order_number}"
    assert _valor(tarea, "link") == link == pedido[0].payment_link_url
    assert link in resultado.text
    transaccion = await _filas(ctx, "select amount from pack_payments_transactions where tenant_id = :t "
                                    "and reference = :r", r=pedido[0].public_order_number)
    assert [float(f.amount) for f in transaccion] == [600000]


async def test_si_el_total_cambia_despues_del_si_se_vuelve_a_pedir(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """La confirmación se ata al resumen que vio el cliente: si la misma cotización cambia de total
    (una promoción aplicada desde otro lado), el sí anterior no vale."""
    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset())
    ctx = await _cliente(tid, pagos)
    await _hasta_el_resumen(ctx, monkeypatch, "premium mensual", "transferencia")
    cotizacion = _valor(await _tarea(ctx), "quote_id")
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            await s.execute(text("update pack_commerce_quotes set discount = 60000, total = 540000 "
                                 "where tenant_id = :t and id = :q"), {"t": tid, "q": cotizacion})
    resultado, guion = await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        "Te muestro el resumen.",
    ])
    assert "confirmation_required" in guion.resultados[-1]
    assert "Total: $540.000" in resultado.text and "Descuento: $60.000" in resultado.text
    tarea = await _tarea(ctx)
    assert tarea.stage == "confirmar"
    estados = await _filas(ctx, "select status from pack_commerce_quotes where tenant_id = :t and id = :q", q=cotizacion)
    assert estados[0].status == "draft"  # todavía nadie aceptó el total nuevo


async def test_cambio_de_plan_antes_de_confirmar_recalcula_y_no_usa_la_vieja(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset())
    ctx = await _cliente(tid, pagos)
    await _hasta_el_resumen(ctx, monkeypatch, "premium mensual", "transferencia")
    vieja = _valor(await _tarea(ctx), "quote_id")
    assert vieja

    resultado, guion = await _turno(ctx, monkeypatch, "Mejor el premium anual", [
        ("actualizar_tarea", {"slots": {"variant_id": "PREMIUM-ANO"}}),
        ("confirmar_cotizacion", {"quote_id": vieja}),      # fuera de etapa: volvió a cotizar
        ("cotizar_plan", {"variant_id": "PREMIUM-MES"}),     # otro plan que el elegido: se niega
        ("cotizar_plan", {"variant_id": "PREMIUM-ANO"}),     # el SKU del elegido vale (resolver)
        ("confirmar_cotizacion", {"quote_id": vieja}),      # ya en confirmar: la vieja no se usa
        ("confirmar_cotizacion", {}),
        "Te muestro el nuevo resumen.",
    ])
    salidas = guion.resultados
    assert "no corresponde a la etapa" in salidas[1]
    assert "no coincide con el dato vigente" in salidas[2]
    assert "no coincide con el dato vigente" in salidas[4]
    tarea = await _tarea(ctx)
    nueva = _valor(tarea, "quote_id")
    assert tarea.stage == "confirmar" and nueva and nueva != vieja
    assert "Total: $6.000.000" in resultado.text and "Periodicidad: anual" in resultado.text

    await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        ("crear_pedido", {"payment_method": "transferencia", "customer_data": {}}),
        "Listo.",
    ])
    tarea = await _tarea(ctx)
    assert tarea.status == "completed"  # plan pago por transferencia: sin link
    anual = await _variante_id(ctx, "PREMIUM-ANO")
    pedido = await _filas(ctx, "select o.total, i.variant_id, o.quote_id from pack_commerce_orders o "
                               "join pack_commerce_order_items i on i.order_id = o.id "
                               "where o.tenant_id = :t and o.customer_id = :c", c=ctx.customer_id)
    assert [(float(f.total), str(f.variant_id), str(f.quote_id)) for f in pedido] == [(6000000, anual, nueva)]
    estados = await _filas(ctx, "select status from pack_commerce_quotes where tenant_id = :t and id = :q", q=vieja)
    assert estados[0].status == "draft"  # la cotización vieja nunca se aceptó ni se convirtió


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


async def test_free_sin_link_y_sin_pago_en_linea(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset())
    ctx = await _cliente(tid, pagos)
    await _turno(ctx, monkeypatch, "Quiero el plan free", [
        ("iniciar_procedimiento", {"procedure_id": PLAN}),
        ("actualizar_tarea", {"slots": {"variant_id": "free"}}),
        ("cotizar_plan", {}),
        ("actualizar_tarea", {"slots": {**DATOS_ANA, "payment_method": "pago_en_linea"}}),
        "Listo.",
    ])
    tarea = await _tarea(ctx)
    assert _valor(tarea, "requiere_pago") is False and _valor(tarea, "email") is None  # todo o nada


async def test_el_negocio_vendedor_pide_sus_datos_antes_de_confirmar_y_los_terminos_en_pagar(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Como solvershub: `checkout_fields` del plan con tipo y nombre del negocio, y los términos del
    alta (ADR-0125) que `crear_pedido` exige en `pagar`, igual que sin procedimientos."""
    from core.plugin_manager import plugin_manager
    from core.presets import list_presets
    from core.shared.tenant import tenant_scope

    tid, pagos = negocio
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset({tid}))
    plan = [*CHECKOUT["plan"], "business_name", "vertical"]
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_commerce", {"checkout_fields": {**CHECKOUT, "plan": plan}})
    await journeys.publicar_documentos_legales(tid, [
        {"tipo": t, "version": "aos07", "url": f"https://solvershub.test/legal/{t}"} for t in ("terminos", "privacidad", "dpa")
    ])
    try:
        ctx = await _cliente(tid, pagos)
        datos = {**journeys.datos_unicos(ctx.customer_id)}
        _, guion = await _turno(ctx, monkeypatch, "Quiero el premium mensual", [
            ("iniciar_procedimiento", {"procedure_id": PLAN}),
            ("actualizar_tarea", {"slots": {"variant_id": "premium mensual"}}),
            ("cotizar_plan", {}),
            ("actualizar_tarea", {"slots": {
                "full_name": "Ana Gómez", "id_document": datos["documento"], "phone": datos["celular"],
                "email": f"ana+{ctx.customer_id.hex[:8]}@example.com", "business_name": "Pizzería Roma",
                "vertical": "no-existe", "payment_method": "transferencia",
            }}),
            "¿Qué tipo de negocio tienes?",
        ])
        assert "Tipo de negocio no válido" in guion.resultados[-1]
        tarea = await _tarea(ctx)
        assert tarea.stage == "datos" and _valor(tarea, "business_name") is None  # todo o nada
        vertical = list_presets()[0].id
        await _turno(ctx, monkeypatch, f"Es un {vertical}", [
            ("actualizar_tarea", {"slots": {
                "full_name": "Ana Gómez", "id_document": datos["documento"], "phone": datos["celular"],
                "email": f"ana+{ctx.customer_id.hex[:8]}@example.com", "business_name": "Pizzería Roma",
                "vertical": vertical, "payment_method": "transferencia",
            }}),
            "¿Confirmas tu correo?",
        ])
        await _turno(ctx, monkeypatch, "Sí, confirmo", [("confirmar_tarea", {}), ("confirmar_cotizacion", {}), "Resumen."])
        assert (await _tarea(ctx)).stage == "confirmar"
        _, guion = await _turno(ctx, monkeypatch, "Sí, confirmo", [
            ("confirmar_tarea", {}),
            ("confirmar_cotizacion", {}),
            ("crear_pedido", {"payment_method": "transferencia", "customer_data": {}}),
            "Antes de seguir, revisa y acepta los documentos.",
        ])
        assert "terminos_pendientes" in guion.resultados[-1]
        token = json.loads(guion.resultados[-1])["acepta_terminos"]
        tarea = await _tarea(ctx)
        assert tarea.stage == "pagar" and tarea.status == "active"
        await _turno(ctx, monkeypatch, "Sí, acepto los términos, el aviso de privacidad y el DPA.", [
            ("crear_pedido", {"payment_method": "transferencia", "customer_data": {"acepta_terminos": token}}),
            "Listo.",
        ])
        tarea = await _tarea(ctx)
        assert tarea.status == "completed"
        pedido = await _filas(ctx, "select datos_alta from pack_commerce_orders where tenant_id = :t "
                                   "and customer_id = :c", c=ctx.customer_id)
        assert pedido[0].datos_alta["vertical"] == vertical and pedido[0].datos_alta["negocio"] == "Pizzería Roma"
    finally:
        async with tenant_scope(tid):
            await plugin_manager.save_config("pack_commerce", {"checkout_fields": CHECKOUT})


async def test_con_la_bandera_apagada_no_se_ofrece_nada_nuevo(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid, pagos = negocio
    await _banderas(tid, False)
    try:
        ctx = await _cliente(tid, pagos)
        _, guion = await _turno(ctx, monkeypatch, "Quiero el premium", ["Claro, te cuento."])
    finally:
        await _banderas(tid, True)
    ofrecidas = set(guion.tools[0])
    assert "generar_cotizacion" in ofrecidas and "crear_pedido" in ofrecidas
    assert "cotizar_plan" not in ofrecidas
    assert not ofrecidas & {"iniciar_procedimiento", "actualizar_tarea", "confirmar_tarea", "cancelar_tarea"}
    assert await _tarea(ctx) is None


async def test_las_frases_de_los_procedimientos_se_verifican_solo_con_el_motor(negocio, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """"Quedó registrado tu pedido" solo lo declara `contratar_plan` (las `COMPLETION_CLAIMS` de
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
