"""Smoke del dueño en test (2026-10-20, negocio `solvershub`, chat web, motor activo).

Con el loop real y un modelo guionado (sin LLM):
- el modelo escribe su propio «Quedó así tu resumen… ¿Confirmas?» al llegar a `confirmar` sin
  llamar `confirmar_cotizacion`: sale la tarjeta del motor en su lugar y el cliente confirma UNA vez;
- un correo con el dominio mal escrito (`gmai.com`) no se guarda sin preguntar;
- el lead del cliente queda con su nombre, correo y negocio;
- el pedido de un plan activado no dice «en preparación»;
- la auditoría de un comando fallido guarda los códigos de error.
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
    slugs = [f"smoke-a-{uuid.uuid4().hex[:6]}", f"smoke-b-{uuid.uuid4().hex[:6]}"]
    a = await _negocio(slugs[0], pagos)
    b = await _negocio(slugs[1], pagos)
    await journeys.publicar_documentos_legales(a, [
        {"tipo": t, "version": "smoke", "url": f"https://solvershub.test/legal/{t}"}
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


async def _con_lead(ctx: Any) -> None:
    """El lead borrador que crea `conversation.started` en producción (sin nombre ni correo)."""
    from pack_commerce.models import Lead

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(ctx.tenant_id):
        async with get_session() as s:
            s.add(Lead(customer_id=ctx.customer_id, conversation_id=ctx.conversation_id, status="nuevo",
                       idempotency_key=f"auto:{ctx.conversation_id}"))


async def _hasta_datos(ctx: Any, monkeypatch: Any) -> None:
    resultado, _ = await _turno(ctx, monkeypatch, "Quiero contratar el plan Premium mensual", [
        ("iniciar_procedimiento", {"procedure_id": PLAN}),
        ("actualizar_tarea", {"slots": {"variant_id": "premium mensual"}}),
        ("cotizar_plan", {}),
        "Plan Premium mensual: $600.000. Necesito estos datos…",
    ])
    assert not resultado.escalated and (await _tarea(ctx)).stage == "datos"


def _todos(datos: dict[str, str], correo: str) -> dict[str, str]:
    return {
        "business_name": "Tienda la rebaja", "vertical": "tienda", "full_name": "Robinson Aguilar",
        "id_document": datos["documento"], "email": correo, "phone": datos["celular"],
        "payment_method": "pago_en_linea",
    }


async def _codigos(ctx: Any) -> list[list[str]]:
    filas = await _filas(ctx.tenant_id, "select output from audit_log where tenant_id = :t and conversation_id = :c "
                                        "and actor = 'core:comando' and action = 'actualizar_tarea' order by created_at",
                         c=ctx.conversation_id)
    return [f.output.get("codigos") for f in filas]


async def test_un_solo_resumen_aunque_el_modelo_escriba_el_suyo(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """La transcripción de 05:13–05:15: los datos llegan juntos, la tarea entra a `confirmar` y el
    modelo escribe su propio resumen sin llamar `confirmar_cotizacion`. Sale la tarjeta del motor;
    el «Si confirmo» siguiente crea el pedido sin una segunda tarjeta."""
    a, _b, pagos = negocios
    monkeypatch.setattr("core.provisioning.vendedores.vendedores_configurados", lambda: frozenset({a}))
    ctx = await _cliente(a, pagos)
    await _con_lead(ctx)
    datos = journeys.datos_unicos(ctx.customer_id)
    correo = f"robinson+{ctx.customer_id.hex[:8]}@tienda.co"
    await _hasta_datos(ctx, monkeypatch)

    mensaje = f"1. Tienda la rebaja. 2. tienda. 3. Robinson Aguilar. 4. {datos['documento']}. 5. {correo}. " \
              f"6. {datos['celular']}. 7. Pago en linea."
    resultado, _ = await _turno(ctx, monkeypatch, mensaje, [
        ("actualizar_tarea", {"slots": _todos(datos, correo)}),
        "Quedó así tu resumen: Plan Premium — $600.000 al mes. ¿Confirmas que lo tomamos así?",
    ])
    assert resultado.confirmacion is not None, resultado.text
    assert resultado.text.startswith("Antes de seguir, revisa que todo esté bien:")
    assert "Quedó así tu resumen" not in resultado.text
    tarea = await _tarea(ctx)
    assert tarea.stage == "confirmar" and tarea.pending_confirmation["kind"] == "action"

    # El lead del cliente quedó con lo que dio (sin crear otro).
    leads = await _filas(a, "select name, email, phone from pack_commerce_leads where tenant_id = :t "
                            "and customer_id = :c", c=ctx.customer_id)
    assert [(lead.name, lead.email, lead.phone) for lead in leads] == [("Robinson Aguilar", correo, datos["celular"])]

    resultado, guion = await _turno(ctx, monkeypatch, "Si confirmo", [
        ("confirmar_tarea", {}),
        ("confirmar_cotizacion", {}),
        ("crear_pedido", {"payment_method": "pago_en_linea", "customer_data": {}}),
        ("confirmar_pago_pedido", {}),
        "Listo, tu pedido quedó creado.",
    ])
    assert resultado.confirmacion is None, resultado.text
    assert (await _tarea(ctx)).stage == "esperar_pago", guion.resultados
    eventos = await _filas(a, "select e.type from task_events e join conversation_tasks t on t.id = e.task_id "
                              "where e.tenant_id = :t and t.conversation_id = :c and e.type = 'confirmation_requested'",
                           c=ctx.conversation_id)
    assert len(eventos) == 1  # una sola petición: la del kernel

    # El pedido de un plan activado no está «en preparación».
    [pedido] = await _filas(a, "select id, public_order_number from pack_commerce_orders where tenant_id = :t "
                               "and customer_id = :c", c=ctx.customer_id)
    await _filas(a, "update pack_commerce_orders set payment_status = 'pagado', plan_activation_status = 'activada' "
                    "where tenant_id = :t and id = :o returning id", o=pedido.id)
    # Como a las 05:18: la tarea ya cerró por el pago y el modelo consulta el pedido.
    from core.mcp.host import mcp_host, new_tool_context
    from core.shared.tenant import tenant_scope

    herramienta = mcp_host.get_tool("consultar_estado_pedido")
    assert herramienta is not None
    async with tenant_scope(a):
        contexto = new_tool_context(conversation_id=ctx.conversation_id, customer_id=ctx.customer_id,
                                    idempotency_key="smoke-estado", channel="test")
        estado = await herramienta.handler({"public_order_number": pedido.public_order_number}, contexto)
    assert estado["estado_plan"] == "activo" and estado["acceso"] == "enviado_al_correo_del_alta"
    assert "status" not in estado and "delivery_method" not in estado


async def test_correo_con_dominio_mal_escrito_se_pregunta(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`devrochy26@gmai.com`: el primer intento se rechaza con la sugerencia (y no se guarda nada,
    todo o nada); reintentarlo en el mismo turno tampoco pasa. Si el cliente insiste en el turno
    siguiente, se acepta; la corrección a `gmail.com` también vale."""
    a, _b, pagos = negocios
    ctx = await _cliente(a, pagos)
    datos = journeys.datos_unicos(ctx.customer_id)
    local = f"devrochy{ctx.customer_id.hex[:6]}"
    malo = f"{local}@gmai.com"
    await _hasta_datos(ctx, monkeypatch)

    _, guion = await _turno(ctx, monkeypatch, f"Robinson Aguilar, {datos['documento']}, {malo}", [
        ("actualizar_tarea", {"slots": {"full_name": "Robinson Aguilar", "id_document": datos["documento"], "email": malo}}),
        ("actualizar_tarea", {"slots": {"full_name": "Robinson Aguilar", "id_document": datos["documento"], "email": malo}}),
        "¿Quisiste decir @gmail.com?",
    ])
    primero = json.loads(guion.resultados[0])
    assert primero["ok"] is False and "@gmail.com" in primero["errors"][0]
    assert json.loads(guion.resultados[1])["ok"] is False
    tarea = await _tarea(ctx)
    assert _valor(tarea, "email") is None and _valor(tarea, "full_name") is None
    assert await _codigos(ctx) == [[], ["correo_dudoso"], ["correo_dudoso"]]

    _, guion = await _turno(ctx, monkeypatch, "No, está bien así", [
        ("actualizar_tarea", {"slots": {"full_name": "Robinson Aguilar", "id_document": datos["documento"], "email": malo}}),
        "Listo.",
    ])
    assert json.loads(guion.resultados[0])["ok"] is True
    assert _valor(await _tarea(ctx), "email") == malo

    # La corrección que propuso el motor también vale aunque el cliente no la escriba entera.
    _, guion = await _turno(ctx, monkeypatch, "Mejor sí, era gmail", [
        ("actualizar_tarea", {"slots": {"email": f"{local}@gmail.com"}}),
        "Listo.",
    ])
    assert json.loads(guion.resultados[0])["ok"] is True, guion.resultados
    assert _valor(await _tarea(ctx), "email") == f"{local}@gmail.com"


async def test_que_etapas_pide_a_confirmar_el_kernel(negocios) -> None:  # type: ignore[no-untyped-def]
    """El punto de reposo de la acción solo aplica donde TODOS los argumentos salen de datos de la
    tarea: contratar un plan y la promesa de pago de cobranza, no el cambio de plan (lleva
    `monto_a_pagar` de la cotización) ni la agenda."""
    from agentcore_contracts import Procedure
    from pack_cobranza.skills import CobranzaSkill

    from core.kernel.procedures import motor
    from core.mcp.host import mcp_host

    de_cobranza = {t.name: t for t in CobranzaSkill().get_tools()}  # el pack no está activo aquí

    def propiedades(accion: str) -> list[str]:
        tool = mcp_host.get_tool(accion) or de_cobranza.get(accion)
        return list(((tool.parameters if tool else {}) or {}).get("properties") or {})

    procs = [Procedure.model_validate_json(f.read_text()) for f in sorted(RAIZ.glob("packages/pack-*/procedures/*.json"))]
    ligables = {proc.id: [a for st in proc.stages if (a := motor.accion_de_salida_ligable(proc, st, propiedades))]
                for proc in procs}
    assert ligables["pack_commerce.contratar_plan"] == ["confirmar_cotizacion"]
    assert ligables["pack_cobranza.promesa_de_pago"] == ["cob_registrar_promesa_saldo"]
    assert ligables["pack_subscriptions.cambiar_plan"] == []
    assert ligables["pack_calendar.agendar_cita"] == []
