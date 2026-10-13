"""Conversación ordenada con el motor (adenda 2026-10-13 de ADR-0113 y de ADR-0125).

Reproduce, con el loop real y un modelo guionado (sin LLM), la segunda prueba real en test (chat
web, negocio `solvershub`, 2026-10-13 05:55–06:09 UTC, `pack_commerce.contratar_plan`): el modelo
pidió los datos de a tandas y con confirmaciones de más, un turno de comandos y negaciones agotó
las vueltas y escaló en falso ("ya mismo te comunico con una persona"), "tienda" no era un tipo de
negocio, el correo se confirmó dos veces, los términos se pidieron DESPUÉS del «confirmo» y el
mensaje final repitió el pedido y el link.

Con el flujo que aprobó el dueño quedan cuatro mensajes del bot: el plan con la lista numerada de
los datos → lo que falta → el resumen único (plan, total, datos del cliente y los documentos que
acepta) → el pedido con el link.
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
# Como `solvershub` en test: el plan pide los datos de la persona y los del negocio.
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
ORDEN = ["business_name", "vertical", "full_name", "id_document", "email", "phone", "payment_method"]


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


def _lista_pedida(system: str) -> list[str]:
    """Los datos de la línea `pide_ahora` de la tarjeta, en orden ("1. business_name (…)")."""
    linea = next((ln for ln in system.splitlines() if ln.startswith("Faltan estos datos, en este orden:")), "")
    return [pieza.split(". ", 1)[1].split(" ", 1)[0] for pieza in linea.split(": ", 1)[-1].split("; ") if ". " in pieza]


async def _hasta_el_resumen(ctx: Any, monkeypatch: Any, datos: dict[str, str]) -> Any:
    """Mensajes 1–3 del bot: el plan con la lista numerada, lo que falta y el resumen único."""
    # 1. El cliente pide el plan: inicia el recorrido, elige y cotiza; el texto del modelo ya ve la
    #    tarjeta de `datos` (el bloque dinámico se refresca al cambiar la etapa) con la lista.
    resultado, guion = await _turno(ctx, monkeypatch, "Quiero contratar el plan Premium mensual", [
        ("iniciar_procedimiento", {"procedure_id": PLAN}),
        ("actualizar_tarea", {"slots": {"variant_id": "premium mensual"}}),
        ("cotizar_plan", {}),
        "Plan Premium mensual: $600.000.\n1. Nombre del negocio\n2. Tipo de negocio\n3. Nombre completo\n"
        "4. Documento\n5. Correo\n6. Teléfono\n7. Forma de pago\nPuedes enviármelos en un solo mensaje.",
    ])
    assert not resultado.escalated and resultado.confirmacion is None
    assert _lista_pedida(guion.system[-1]) == ORDEN, guion.system[-1]
    assert "puedes enviármelos en un solo mensaje" in guion.system[-1]
    assert (await _tarea(ctx)).stage == "datos"

    # 2. Manda una parte (con "tienda" como tipo): se guarda todo junto y se pide solo lo que falta.
    parte = f"Tienda Rob, es una tienda. Roberto Pérez, {datos['documento']}, {datos['correo']}"
    resultado, guion = await _turno(ctx, monkeypatch, parte, [
        ("actualizar_tarea", {"slots": {
            "business_name": "Tienda Rob", "vertical": "tienda", "full_name": "Roberto Pérez",
            "id_document": datos["documento"], "email": datos["correo"],
        }}),
        "Gracias. Me faltan:\n1. Teléfono\n2. Forma de pago",
    ])
    tarea = await _tarea(ctx)
    assert _valor(tarea, "vertical") == "comercio"  # "tienda" es un sinónimo del preset
    assert _lista_pedida(guion.system[-1]) == ["phone", "payment_method"]
    assert tarea.pending_confirmation is None  # el correo no se pregunta aparte

    # 3. El resto: no falta nada en `datos` y sale el resumen único del kernel.
    resultado, guion = await _turno(ctx, monkeypatch, f"{datos['celular']}, pago en línea", [
        ("actualizar_tarea", {"slots": {"phone": datos["celular"], "payment_method": "pago_en_linea"}}),
        ("confirmar_cotizacion", {}),
        "¿Confirmas que tu correo está bien?",
    ])
    assert resultado.confirmacion is not None and resultado.confirmacion.modo == "interactive_button"
    return resultado


async def test_la_transcripcion_queda_en_cuatro_mensajes(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.provisioning.antiabuso import pedido_de_alta
    from core.shared.tenant import tenant_scope

    a, _b, pagos = negocios
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset({a}))
    ctx = await _cliente(a, pagos)
    datos = {**journeys.datos_unicos(ctx.customer_id), "correo": f"rob+{ctx.customer_id.hex[:8]}@tienda.co"}
    resultado = await _hasta_el_resumen(ctx, monkeypatch, datos)

    lineas = resultado.text.splitlines()
    assert lineas[:3] == ["Antes de seguir, revisa que todo esté bien:", "• Plan: Premium mensual", "• Total: $600.000"]
    for linea in ("• Nombre del negocio: Tienda Rob", "• Tipo de negocio: comercio", "• Nombre completo: Roberto Pérez",
                  f"• Correo: {datos['correo']}", "• Forma de pago: Pago en línea"):
        assert linea in lineas, resultado.text
    assert all(f"https://solvershub.test/legal/{t}" in resultado.text for t in ("terminos", "privacidad", "dpa"))
    assert lineas[-1].startswith("Si todo está bien y aceptas los términos, responde *Sí, confirmo*")
    assert "¿Confirmas que tu correo" not in resultado.text  # el texto del modelo no sale
    tarea = await _tarea(ctx)
    eventos = await _filas(a, "select type, data from task_events where tenant_id = :t and task_id = :k order by seq",
                           k=tarea.id)
    pedidas = [e.data["target"] for e in eventos if e.type == "confirmation_requested"]
    assert pedidas == [{"kind": "action", "name": "confirmar_cotizacion"}]  # una sola confirmación

    # 4. «Sí, confirmo»: acepta el resumen y los términos; un solo mensaje con el pedido y el link.
    def _texto_final(resultados: list[str]) -> str:
        link = json.loads(resultados[-1])["link"]
        return f"¡Listo! Tu pedido quedó creado. Paga aquí: {link}"

    resultado, guion = await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        ("crear_pedido", {"payment_method": "pago_en_linea", "customer_data": {}}),
        ("confirmar_pago_pedido", {}),
        _texto_final,
    ])
    assert "terminos_pendientes" not in "".join(guion.resultados), guion.resultados
    tarea = await _tarea(ctx)
    assert (tarea.stage, tarea.status) == ("esperar_pago", "active"), guion.resultados
    link = _valor(tarea, "link")
    assert resultado.text.count(link) == 1 and resultado.confirmacion is None
    # Un solo mensaje: el link una vez, con el texto exacto del core; sin el "✅ quedó confirmado".
    assert "✅" not in resultado.text and "600,000" not in resultado.text
    assert resultado.text.endswith(f"aquí: {link}")

    [pedido] = await _filas(a, "select public_order_number from pack_commerce_orders where tenant_id = :t "
                               "and customer_id = :c", c=ctx.customer_id)
    async with tenant_scope(a):
        registro = await pedido_de_alta(a, pedido.public_order_number)
    [confirmo] = await _filas(a, "select id from messages where tenant_id = :t and conversation_id = :c and "
                                 "direction = 'inbound' and content = 'Sí, confirmo'", c=ctx.conversation_id)
    assert registro is not None and registro.aceptacion["mensaje_id"] == str(confirmo.id)
    salientes = await _filas(a, "select content from messages where tenant_id = :t and conversation_id = :c "
                                "and direction = 'outbound'", c=ctx.conversation_id)
    assert len(salientes) == 4


async def test_comandos_y_negaciones_no_escalan_en_falso(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """El turno del "Si" de la transcripción: comandos, consultas y dos negaciones del kernel. Los
    comandos no gastan vueltas y, si se agotan con la tarea en foco, el kernel pide lo que falta
    en vez de decir "te comunico con una persona"."""
    a, _b, pagos = negocios
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset({a}))
    ctx = await _cliente(a, pagos)
    datos = {**journeys.datos_unicos(ctx.customer_id), "correo": f"rob+{ctx.customer_id.hex[:8]}@tienda.co"}
    await _turno(ctx, monkeypatch, f"Tienda Rob, es una tienda, mi correo es {datos['correo']}, quiero el Premium", [
        "¿Me confirmas que el correo es correcto?",
    ])
    pasos: list[Paso] = [
        ("iniciar_procedimiento", {"procedure_id": PLAN}),
        ("consultar_inventario", {}),
        ("informar_plan", {"sku": "PREMIUM-MES"}),
        ("actualizar_tarea", {"slots": {"vertical": "astronautas"}}),  # error del resolver
        ("actualizar_tarea", {"slots": {
            "variant_id": "premium", "business_name": "Tienda Rob", "vertical": "tienda", "email": datos["correo"],
        }}),
        ("cotizar_plan", {}),
        ("aplicar_promocion", {"code": "X"}),  # negada: fuera de etapa (`datos`)
        ("cotizar_plan", {}),                   # negada: fuera de etapa
    ]
    resultado, guion = await _turno(ctx, monkeypatch, "Si", pasos, por_defecto=("consultar_inventario", {}))
    assert not resultado.escalated, resultado.text
    assert resultado.text == (
        "Para seguir necesito estos datos:\n1. Nombre completo\n2. Documento\n3. Teléfono\n4. Forma de pago\n\n"
        "Puedes enviármelos en un solo mensaje."
    )
    tarea = await _tarea(ctx)
    assert tarea.stage == "datos" and tarea.status == "active"
    assert await _filas(a, "select id from escalations where tenant_id = :t and conversation_id = :c",
                        c=ctx.conversation_id) == []
    sin_vueltas = await _filas(a, "select input from audit_log where tenant_id = :t and conversation_id = :c "
                                  "and actor = 'core:motor' and action = 'sin_vueltas'", c=ctx.conversation_id)
    assert [f.input for f in sin_vueltas] == [{"salida": "pregunta"}]
    # Las tres vueltas de solo comandos no gastaron el tope (10 del spec por defecto).
    from core.kernel.procedures.ejecucion import VUELTAS_LIBRES_DE_COMANDOS

    assert len(guion.tools) == 10 + VUELTAS_LIBRES_DE_COMANDOS


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


async def test_dos_negocios_no_se_cruzan(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """B (sin términos, no vendedor) recorre lo mismo a la vez que A: sus tareas, su resumen y su
    pedido son suyos; el «Sí, confirmo» de A no confirma nada en B ni lleva los documentos de A."""
    a, b, pagos = negocios
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset({a}))
    ctx_a = await _cliente(a, pagos)
    ctx_b = await _cliente(b, pagos, nombre="Ana")
    datos_a = {**journeys.datos_unicos(ctx_a.customer_id), "correo": f"rob+{ctx_a.customer_id.hex[:8]}@tienda.co"}
    datos_b = {**journeys.datos_unicos(ctx_b.customer_id), "correo": f"ana+{ctx_b.customer_id.hex[:8]}@tienda.co"}
    resumen_a = await _hasta_el_resumen(ctx_a, monkeypatch, datos_a)
    resumen_b = await _hasta_el_resumen(ctx_b, monkeypatch, datos_b)
    assert "solvershub.test/legal" in resumen_a.text and "solvershub.test/legal" not in resumen_b.text
    assert datos_a["correo"] not in resumen_b.text and datos_b["correo"] not in resumen_a.text
    assert resumen_b.text.splitlines()[-1].startswith("Si todo está bien, responde *Sí, confirmo*")

    await _turno(ctx_a, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        ("crear_pedido", {"payment_method": "pago_en_linea", "customer_data": {}}),
        ("confirmar_pago_pedido", {}),
        "Listo.",
    ])
    assert (await _tarea(ctx_a)).stage == "esperar_pago"
    tarea_b = await _tarea(ctx_b)
    assert tarea_b.stage == "confirmar" and tarea_b.pending_confirmation["kind"] == "action"
    assert await _filas(b, "select id from pack_commerce_orders where tenant_id = :t", ) == []
    # Desde B no se ven las tareas ni los pedidos de A (RLS y filtro explícito).
    assert await _filas(b, "select id from conversation_tasks where conversation_id = :c", c=ctx_a.conversation_id) == []
    assert await _filas(b, "select id from pack_commerce_orders where customer_id = :c", c=ctx_a.customer_id) == []


async def test_un_si_a_sin_resumen_no_confirma_ni_acepta_terminos(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Revisión de seguridad: si el resumen no se pudo mostrar (sale `sin_resumen`), la petición
    se anula. Un «Sí, confirmo» a ese texto no ejecuta la acción ni acepta términos que el cliente
    no vio: la acción vuelve a pedir el sí con su resumen."""
    import core.kernel.confirmacion as confirmacion

    a, _b, pagos = negocios
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset({a}))
    ctx = await _cliente(a, pagos)
    datos = {**journeys.datos_unicos(ctx.customer_id), "correo": f"rob+{ctx.customer_id.hex[:8]}@tienda.co"}

    def _falla(*_: Any, **__: Any) -> Any:
        raise confirmacion.ResumenInvalido("simulado")

    with monkeypatch.context() as roto:
        roto.setattr(confirmacion, "armar", _falla)
        with pytest.raises(AssertionError):  # el tercer mensaje sale `sin_resumen`, sin botón
            await _hasta_el_resumen(ctx, monkeypatch, datos)
    tarea = await _tarea(ctx)
    assert tarea.stage == "confirmar" and tarea.pending_confirmation["kind"] == "action"

    resultado, guion = await _turno(ctx, monkeypatch, "Sí, confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        "Te muestro el resumen.",
    ])
    assert "confirmation_required" in guion.resultados[-1]
    assert (await _tarea(ctx)).stage == "confirmar"
    assert resultado.confirmacion is not None and "https://solvershub.test/legal/dpa" in resultado.text
    assert await _filas(a, "select id from pack_commerce_orders where tenant_id = :t and customer_id = :c",
                        c=ctx.customer_id) == []
