"""Motor de procedimientos contra el stack real (HU-AOS-05, ADR-0113 §1).

Gherkin de la HU:
- **Acciones por etapa:** con la tarea en "datos", una acción de otra etapa no se ofrece y, si
  llega igual, se deniega con un error de negocio (sin ejecutarse).
- **Datos validados:** `actualizar_tarea` con un plan inexistente → error claro del resolver y el
  dato no se guarda.
- **Confirmación:** "dale" no confirma; el botón sí. La confirmación de una acción queda atada a
  sus argumentos.
- **Cierre por evento:** `payment.confirmed` del pedido de la tarea, por el outbox → `completed`
  (y no toca otro negocio ni otro pedido).
- **Datos sin LLM:** botón de lista (con candidatos), imagen; un botón tras un episodio vencido no
  resucita la tarea.
- **Modo sombra:** con `aos_runtime = v3` y `aos_procedures` apagada, el prompt, las acciones y la
  respuesta son idénticos y quedan las diferencias en `audit_log`.

Modelo guionado (sin LLM real). Los procedimientos son de un pack de prueba registrado en el
host del proceso.
"""

from __future__ import annotations

import json
import os
import types
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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

PACK = "pack_prueba_motor"
PROC_ID = f"{PACK}.compra"
LLAMADAS: list[str] = []

PROCEDIMIENTO = {
    "id": PROC_ID,
    "version": "1.0.0",
    "title": "Compra de prueba",
    "slots": {
        "plan": {"type": "string", "required": True, "resolver": f"{PACK}.plan"},
        "correo": {"type": "string", "format": "email", "required": True, "confirm": True, "dataClass": "pii"},
        "opcion": {"type": "string", "setBy": "interactive:opcion:", "candidates": {"source": "action:ver_opciones_m.opciones"}},
        "foto": {"type": "media", "setBy": "content:image"},
        "reference": {"type": "string", "setBy": "action:crear_pedido_m"},
    },
    "stages": [
        {"id": "datos", "actions": ["ver_opciones_m"], "exitWhen": {"slots": ["plan", "correo"], "confirmed": ["correo"]}},
        {"id": "confirmar", "actions": ["cotizar_m", "aceptar_m"], "confirmation": "interactive_button",
         "exitWhen": {"action": "aceptar_m", "ok": True}},
        {"id": "pagar", "actions": ["crear_pedido_m"], "exitWhen": {"slots": ["reference"]}},
    ],
    "completion": {"events": ["core.payment.confirmed"]},
}


@asynccontextmanager
async def _scope(tid: uuid.UUID | None) -> AsyncIterator[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            yield session


@asynccontextmanager
async def _owner() -> AsyncIterator[Any]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            yield conn
    finally:
        await engine.dispose()


async def _banderas(tid: uuid.UUID, runtime: str, procedimientos: bool) -> None:
    async with _owner() as conn:
        await conn.execute(text("update tenants set aos_procedures = false where id = :t"), {"t": tid})
        await conn.execute(
            text("update tenants set aos_runtime = :r, aos_procedures = :p where id = :t"),
            {"t": tid, "r": runtime, "p": procedimientos},
        )


# ---------------------------------------------------------------- pack de prueba


def _herramienta(nombre: str, resultado: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
    from core.mcp.types import MCPTool

    async def handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        LLAMADAS.append(nombre)
        return dict(resultado)

    return MCPTool(
        name=nombre, description=f"prueba {nombre}",
        parameters=params or {"type": "object", "properties": {}}, handler=handler,
    )


@pytest.fixture(scope="module")
def pack_de_prueba() -> Any:
    from agentcore_contracts import Procedure

    from core.mcp.host import mcp_host
    from core.mcp.types import BaseSkill, ResolverResult

    class _Skill(BaseSkill):
        name = "prueba_motor"

        def get_tools(self) -> list[Any]:
            return [
                _herramienta("ver_opciones_m", {"opciones": [{"id": "a"}, {"id": "b"}]}),
                _herramienta("cotizar_m", {"total": 100}),
                _herramienta("aceptar_m", {"ok": True}, {"type": "object", "properties": {"cotizacion": {"type": "string"}}}),
                _herramienta("crear_pedido_m", {"reference": "REF-1"}),
            ]

    async def validar_plan(valor: Any, ctx: Any, datos: dict[str, Any]) -> ResolverResult:
        if valor in ("basico", "premium"):
            return ResolverResult(ok=True, value=f"var-{valor}")
        return ResolverResult(ok=False, error="Ese plan no existe: los planes son básico y premium.")

    mcp_host.begin_activation(PACK)
    mcp_host.register_skill(_Skill())
    mcp_host.register_procedures([Procedure.model_validate(PROCEDIMIENTO)])
    mcp_host.register_resolver(f"{PACK}.plan", validar=validar_plan)
    mcp_host.end_activation()
    yield
    mcp_host.remove_plugin(PACK)


@pytest.fixture(autouse=True)
def _habilitar(pack_de_prueba: Any, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.kernel.procedures import registro

    monkeypatch.setattr(registro, "_valido", lambda proc: True)  # sus acciones no están en el catálogo del binario
    monkeypatch.setattr("core.orchestrator.loop.enabled_packs", lambda spec: {"core", PACK})

    async def permitidos(tenant_id: uuid.UUID) -> set[str]:
        return {"core", PACK}

    monkeypatch.setattr(registro, "packs_permitidos", permitidos)  # consumer y worker
    LLAMADAS.clear()


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[dict[str, uuid.UUID]]:
    from core.alta_negocio import onboard_tenant
    from core.tenants import deactivate_tenant, purge_tenant

    creados: list[tuple[uuid.UUID, str]] = []
    for _ in range(2):
        slug = f"motor-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Motor {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
        creados.append((tid, slug))
    yield {"a": creados[0][0], "b": creados[1][0]}
    for tid, slug in creados:
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _conversacion(tid: uuid.UUID, mensaje: str = "hola", cuando: datetime | None = None) -> Any:
    from core.db.models import Conversation, Customer, Message

    async with _scope(tid) as s:
        cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"motor:{uuid.uuid4().hex[:8]}")
        s.add(cliente)
        await s.flush()
        conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel="whatsapp")
        s.add(conv)
        await s.flush()
        s.add(Message(tenant_id=tid, conversation_id=conv.id, direction="inbound", sender_type="customer",
                      content=mensaje, created_at=cuando or datetime.now(UTC)))
        return types.SimpleNamespace(id=conv.id, tenant_id=tid, customer_id=cliente.id, channel="whatsapp")


async def _mensaje(conv: Any, direccion: str, texto: str, cuando: datetime) -> None:
    from core.db.models import Message

    async with _scope(conv.tenant_id) as s:
        s.add(Message(tenant_id=conv.tenant_id, conversation_id=conv.id, direction=direccion,
                      sender_type="customer" if direccion == "inbound" else "agent", content=texto, created_at=cuando))


async def _tarea(conv: Any, stage: str = "datos", now: datetime | None = None, **slots: Any) -> uuid.UUID:
    from core.kernel import state

    async with _scope(conv.tenant_id) as s:
        fila = await state.iniciar(
            s, tenant_id=conv.tenant_id, conversation_id=conv.id, procedure_id=PROC_ID, procedure_version="1.0.0",
            stage=stage, actor="kernel", now=now,
        )
        for nombre, (valor, fuente, confirmado) in slots.items():
            await state.registrar(
                s, tenant_id=conv.tenant_id, conversation_id=conv.id, task_id=fila.id, tipo="slot_set",
                data={"slot": nombre, "value": valor, "source": fuente, "data_class": "public", "confirmed": confirmado},
                actor="kernel",
            )
        return fila.id


async def _proyeccion(conv: Any, task: uuid.UUID) -> Any:
    from core.db.models import ConversationTask
    from core.kernel import state

    async with _scope(conv.tenant_id) as s:
        fila = await s.get(ConversationTask, task)
        assert fila is not None
        assert state.proyeccion_de(fila) == await state.reconstruir(
            s, tenant_id=conv.tenant_id, conversation_id=conv.id, task_id=task
        )
        return state.proyeccion_de(fila)


async def _tipos(conv: Any, task: uuid.UUID) -> list[str]:
    from core.kernel import state

    async with _scope(conv.tenant_id) as s:
        return [e.type for e in await state.eventos_de_tarea(s, tenant_id=conv.tenant_id, conversation_id=conv.id, task_id=task)]


async def _motor(conv: Any, texto: str = "hola") -> Any:
    from core.kernel.procedures.ejecucion import contexto_del_motor
    from core.mcp.host import mcp_host
    from core.shared.tenant import tenant_scope

    herramientas = mcp_host.get_skill_tools(["prueba_motor"])
    async with tenant_scope(conv.tenant_id):
        motor = await contexto_del_motor(
            conv.id, customer_id=conv.customer_id, channel="whatsapp", enabled={"core", PACK}, disabled=set(),
            herramientas_base=herramientas, candidatas=herramientas, texto_cliente=texto,
        )
    assert motor is not None
    return motor


class _Guion:
    """Modelo guionado: devuelve las respuestas en orden y guarda lo que vio."""

    provider_name = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Any]) -> None:
        self.pasos = list(pasos)
        self.system: list[list[str]] = []
        self.tools: list[list[str]] = []
        self.mensajes: list[list[Any]] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        self.system.append(list(kwargs["system_blocks"]))
        self.tools.append(sorted(t.name for t in (kwargs.get("tools") or [])))
        self.mensajes.append(list(kwargs["messages"]))
        paso = self.pasos.pop(0) if self.pasos else "Listo."
        usage = types.SimpleNamespace(input_tokens=5, output_tokens=5)
        if isinstance(paso, tuple):
            from core.llm.types import LLMToolCall

            nombre, args = paso
            return types.SimpleNamespace(
                content="", stop_reason="tool_use", model="fake", latency_ms=1, usage=usage,
                tool_calls=[LLMToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=nombre, arguments=args)],
            )
        return types.SimpleNamespace(content=paso, stop_reason="end_turn", tool_calls=[], model="fake", latency_ms=1, usage=usage)


async def _turno(conv: Any, guion: _Guion, monkeypatch, texto: str = "hola") -> Any:  # type: ignore[no-untyped-def]
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    async with tenant_scope(conv.tenant_id):
        return await process_conversation(conv.id, texto)


async def _comando(conv: Any, motor: Any, nombre: str, args: dict[str, Any]) -> Any:
    from core.shared.tenant import tenant_scope

    async with tenant_scope(conv.tenant_id):
        return await motor.aplicar_comando(nombre, args)


async def _entrada(conv: Any, fields: dict[str, str]) -> None:
    from core.kernel.procedures.ejecucion import entrada_sin_llm
    from core.shared.tenant import tenant_scope

    async with tenant_scope(conv.tenant_id):
        await entrada_sin_llm(conv, fields)


def _resultado_de_tool(guion: _Guion, llamada: int) -> str:
    """Contenido del último mensaje `tool` que vio el modelo en la llamada N."""
    return next(m.content for m in reversed(guion.mensajes[llamada]) if m.role == "tool")


# ---------------------------------------------------------------- Gherkin


async def test_accion_fuera_de_etapa_no_se_ofrece_y_se_deniega(negocios: dict[str, uuid.UUID], monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a = negocios["a"]
    await _banderas(a, "v3", True)
    try:
        conv = await _conversacion(a, "quiero comprar")
        task = await _tarea(conv)
        guion = _Guion([("crear_pedido_m", {}), "Primero necesito tu correo."])
        resultado = await _turno(conv, guion, monkeypatch, "quiero comprar")
    finally:
        await _banderas(a, "legacy", False)
    assert "crear_pedido_m" not in guion.tools[0] and "ver_opciones_m" in guion.tools[0]
    assert "no corresponde a la etapa actual (datos)" in _resultado_de_tool(guion, 1)
    assert "crear_pedido_m" not in LLAMADAS  # nunca se ejecutó
    assert resultado is not None and resultado.text == "Primero necesito tu correo."
    assert (await _proyeccion(conv, task)).stage == "datos"
    async with _scope(a) as s:
        accion = (await s.execute(text(
            "select action from audit_log where conversation_id = :c and actor = 'tool:crear_pedido_m'"
        ), {"c": conv.id})).scalar_one()
    assert accion == "denied"
    # La tarjeta llega con la definición del procedimiento.
    dinamico = guion.system[0][-1]
    assert '"Compra de prueba"' in dinamico and "1/3 datos" in dinamico and "plan: FALTA" in dinamico


async def test_resolver_rechaza_el_plan_inexistente_y_no_guarda(negocios: dict[str, uuid.UUID]) -> None:
    a = negocios["a"]
    await _banderas(a, "v3", True)
    try:
        conv = await _conversacion(a)
        task = await _tarea(conv)
        motor = await _motor(conv)
        r = await _comando(conv, motor, "actualizar_tarea", {"slots": {"plan": "oro", "correo": "ana@correo.co"}})
        assert not r.ok and r.errores == ["plan: Ese plan no existe: los planes son básico y premium."]
        r2 = await _comando(conv, motor, "actualizar_tarea", {"slots": {"correo": "no-es-correo", "reference": "X"}})
        assert not r2.ok and len(r2.errores) == 2 and any("lo fija el sistema" in e for e in r2.errores)
        assert await _tipos(conv, task) == ["started"]  # todo o nada: nada se guardó

        r3 = await _comando(conv, motor, "actualizar_tarea", {"slots": {"plan": "premium", "correo": "Ana@Correo.co"}})
        assert r3.ok
    finally:
        await _banderas(a, "legacy", False)
    p = await _proyeccion(conv, task)
    assert p.slots["plan"]["value"] == "var-premium"  # el valor normalizado del resolver
    assert p.slots["correo"]["value"] == "ana@correo.co"
    assert p.pending_confirmation is not None and p.pending_confirmation["name"] == "correo"
    assert p.stage == "datos"  # falta confirmar el correo


async def _boton(conv: Any, task: uuid.UUID, *, seq_extra: int = 0) -> str:
    """El botón firmado de la petición pendiente de la tarea (lo que enviaría HU-AOS-09)."""
    from core.kernel.procedures.ejecucion import payload_de_confirmacion

    p = await _proyeccion(conv, task)
    assert p.pending_confirmation is not None
    seq = p.pending_confirmation["seq"]
    async with _scope(conv.tenant_id) as s:
        args_hash = (await s.execute(text(
            "select data->>'args_hash' from task_events where task_id = :t and seq = :q"
        ), {"t": task, "q": seq})).scalar_one()
    return payload_de_confirmacion(conv.tenant_id, task, seq + seq_extra, args_hash or "")


async def test_dale_no_confirma_y_el_si_escrito_solo_contesta_la_peticion_anterior(negocios: dict[str, uuid.UUID]) -> None:
    a = negocios["a"]
    await _banderas(a, "v3", True)
    try:
        conv = await _conversacion(a, "mi correo es a@b.co", datetime.now(UTC) - timedelta(minutes=5))
        task = await _tarea(conv, plan=("var-basico", "llm", False))
        motor = await _motor(conv, "mi correo es a@b.co")
        assert (await _comando(conv, motor, "actualizar_tarea", {"slots": {"correo": "a@b.co"}})).ok

        # Mismo turno en que se pidió: ni un "sí, confirmo" sirve (la evidencia es anterior a la petición).
        motor.texto_cliente = "sí, confirmo"
        assert not (await _comando(conv, motor, "confirmar_tarea", {})).ok
        await _mensaje(conv, "outbound", "¿Confirmas que tu correo es a@b.co?", datetime.now(UTC))
        await _mensaje(conv, "inbound", "dale", datetime.now(UTC))
        # Turno siguiente, "dale": ambiguo → no avanza y se vuelve a pedir.
        r = await _comando(conv, motor := await _motor(conv, "dale"), "confirmar_tarea", {})
        assert not r.ok and "Falta la confirmación explícita" in r.errores[0]
        assert "confirmed" not in await _tipos(conv, task)

        # Un botón vencido no confirma y su título tampoco cuenta como un sí escrito.
        await _entrada(conv, {"interactive_id": await _boton(conv, task, seq_extra=9)})
        assert not (await _comando(conv, await _motor(conv, "Sí, confirmo"), "confirmar_tarea", {})).ok
        assert "confirmed" not in await _tipos(conv, task)

        # Un sí explícito que contesta la petición del turno anterior sí confirma.
        r = await _comando(conv, await _motor(conv, "Sí, confirmo"), "confirmar_tarea", {})
    finally:
        await _banderas(a, "legacy", False)
    assert r.ok, r.errores
    p = await _proyeccion(conv, task)
    assert p.slots["correo"]["confirmed"] is True and p.pending_confirmation is None


async def test_el_boton_firmado_confirma_sin_llm_y_uno_falso_no(negocios: dict[str, uuid.UUID]) -> None:
    from core.kernel.procedures.motor import PREFIJO_CONFIRMAR

    a = negocios["a"]
    await _banderas(a, "v3", True)
    try:
        conv = await _conversacion(a)
        task = await _tarea(conv, plan=("var-basico", "llm", False))
        motor = await _motor(conv)
        assert (await _comando(conv, motor, "actualizar_tarea", {"slots": {"correo": "a@b.co"}})).ok
        bueno = await _boton(conv, task)
        falso = bueno.rsplit(":", 1)[0] + ":" + "0" * 16
        assert falso.startswith(PREFIJO_CONFIRMAR)
        await _entrada(conv, {"interactive_id": falso})  # misma petición, firma fabricada
        assert "confirmed" not in await _tipos(conv, task)
        await _entrada(conv, {"interactive_id": bueno})
    finally:
        await _banderas(a, "legacy", False)
    p = await _proyeccion(conv, task)
    assert p.slots["correo"]["confirmed"] is True and p.pending_confirmation is None
    assert p.stage == "datos"  # el consumer no transiciona: avanza el turno siguiente


async def test_confirmacion_de_accion_atada_a_sus_argumentos_y_avance(negocios: dict[str, uuid.UUID], monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a = negocios["a"]
    await _banderas(a, "v3", True)
    try:
        conv = await _conversacion(a)
        task = await _tarea(conv, "confirmar")
        guion = _Guion([("aceptar_m", {"cotizacion": "A"}), "¿Confirmas la cotización A?"])
        await _turno(conv, guion, monkeypatch)
        assert "aceptar_m" not in LLAMADAS and "confirmation_required" in _resultado_de_tool(guion, 1)
        await _entrada(conv, {"interactive_id": await _boton(conv, task)})

        # Confirmó A; el modelo intenta B → vuelve a pedir confirmación. Luego A → se ejecuta y avanza.
        guion = _Guion([("aceptar_m", {"cotizacion": "B"}), ("aceptar_m", {"cotizacion": "A"}), ("crear_pedido_m", {}), "Listo."])
        await _turno(conv, guion, monkeypatch)
    finally:
        await _banderas(a, "legacy", False)
    assert "confirmation_required" in _resultado_de_tool(guion, 1)
    assert LLAMADAS == ["aceptar_m", "crear_pedido_m"]
    assert "crear_pedido_m" not in guion.tools[0] and "crear_pedido_m" in guion.tools[2]  # acciones de la etapa nueva
    p = await _proyeccion(conv, task)
    assert p.stage == "pagar" and p.status == "active"  # espera el evento de pago
    assert p.slots["reference"] == {**p.slots["reference"], "value": "REF-1", "source": "action:crear_pedido_m"}


async def test_cierre_por_evento_del_outbox_solo_del_pedido_de_la_tarea(negocios: dict[str, uuid.UUID]) -> None:
    from core.events.outbox import emit_domain
    from core.tasks.domain_events_dispatch import entregar_evento

    a, b = negocios["a"], negocios["b"]
    tareas: dict[str, tuple[Any, uuid.UUID]] = {}
    for nombre, tid, ref in (("a", a, "REF-77"), ("otro_pedido", a, "REF-78"), ("b", b, "REF-77")):
        await _banderas(tid, "v3", True)
        conv = await _conversacion(tid)
        tareas[nombre] = (conv, await _tarea(conv, "pagar", reference=(ref, "action:crear_pedido_m", False)))
    conv_cliente = await _conversacion(a)
    tarea_cliente = await _tarea(conv_cliente, "pagar", reference=("REF-77", "llm", False))  # la escribió el modelo
    try:
        async with _scope(a) as s:
            await emit_domain(s, tenant_id=a, topic="payment.confirmed", dedupe_key=f"t:{uuid.uuid4()}",
                              payload={"reference": "REF-77", "provider": "x", "provider_reference": "p", "amount": 1})
        async with _scope(a) as s:
            evento = (await s.execute(text(
                "select id from domain_events where tenant_id = :t and topic = 'payment.confirmed' and status = 'pendiente'"
            ), {"t": a})).scalar_one()
        assert await entregar_evento(evento, a, datetime.now(UTC))
    finally:
        await _banderas(a, "legacy", False)
        await _banderas(b, "legacy", False)
    conv, task = tareas["a"]
    p = await _proyeccion(conv, task)
    assert p.status == "completed" and p.completed_at is not None
    assert (await _proyeccion(*tareas["otro_pedido"])).status == "active"
    assert (await _proyeccion(*tareas["b"])).status == "active"  # otro negocio, mismo número
    assert (await _proyeccion(conv_cliente, tarea_cliente)).status == "active"  # dato del modelo: no correlaciona
    async with _scope(a) as s:
        assert (await s.execute(text("select status from domain_events where id = :e"), {"e": evento})).scalar_one() == "entregado"


async def test_datos_sin_llm_boton_de_lista_e_imagen(negocios: dict[str, uuid.UUID], monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a = negocios["a"]
    await _banderas(a, "v3", True)
    try:
        conv = await _conversacion(a)
        task = await _tarea(conv)
        await _entrada(conv, {"interactive_id": "opcion:a"})  # aún no se ofreció nada
        assert "opcion" not in (await _proyeccion(conv, task)).slots
        await _turno(conv, _Guion([("ver_opciones_m", {}), "Elige una opción."]), monkeypatch)
        await _entrada(conv, {"interactive_id": "opcion:z"})  # no estaba entre las ofrecidas
        assert "opcion" not in (await _proyeccion(conv, task)).slots
        await _entrada(conv, {"interactive_id": "opcion:b",
                                     "media": json.dumps([{"type": "image/jpeg", "id": "wamid-1", "url": ""}])})
    finally:
        await _banderas(a, "legacy", False)
    p = await _proyeccion(conv, task)
    assert p.slots["opcion"]["value"] == "b" and p.slots["opcion"]["source"] == "interactive:opcion:"
    assert p.slots["foto"]["value"] == {"media_id": "wamid-1", "mime": "image/jpeg"}
    assert p.slots["foto"]["source"] == "content:image"


async def test_boton_tras_un_episodio_vencido_no_resucita_la_tarea(negocios: dict[str, uuid.UUID]) -> None:
    """Una tarea sin actividad en todo el episodio anterior expira al abrir el nuevo, también si
    el nuevo empieza con un botón (el consumer abre el episodio antes de tocar la tarea)."""
    from core.kernel.procedures.ejecucion import contexto_del_motor
    from core.shared.tenant import tenant_scope

    a = negocios["a"]
    t1, t2 = datetime.now(UTC) - timedelta(days=4), datetime.now(UTC) - timedelta(days=2)

    async def turno(conv: Any) -> None:
        async with tenant_scope(a):
            await contexto_del_motor(conv.id, customer_id=conv.customer_id, channel="whatsapp", enabled={"core", PACK},
                                     disabled=set(), herramientas_base=[], candidatas=[], texto_cliente="hola")

    await _banderas(a, "v3", True)
    try:
        conv = await _conversacion(a, "hola", t1)
        await turno(conv)  # episodio 1
        task = await _tarea(conv, now=t1 + timedelta(seconds=2))
        await _mensaje(conv, "outbound", "¿Qué plan quieres?", t1 + timedelta(seconds=5))
        await _mensaje(conv, "inbound", "sigo aquí", t2)
        await turno(conv)  # episodio 2: la tarea tuvo actividad en el 1, sigue abierta
        assert (await _proyeccion(conv, task)).status == "active"
        await _mensaje(conv, "outbound", "Te escucho", t2 + timedelta(seconds=5))
        await _mensaje(conv, "inbound", "Opción B", datetime.now(UTC))
        await _entrada(conv, {"interactive_id": "opcion:b"})  # episodio 3 empieza con un botón
    finally:
        await _banderas(a, "legacy", False)
    p = await _proyeccion(conv, task)
    assert p.status == "cancelled" and p.status_reason == "episode_expired" and "opcion" not in p.slots


async def test_modo_sombra_no_cambia_nada_y_registra_diferencias(negocios: dict[str, uuid.UUID], monkeypatch) -> None:  # type: ignore[no-untyped-def]
    b = negocios["b"]
    vistos: dict[str, _Guion] = {}
    for runtime in ("legacy", "v3"):
        await _banderas(b, runtime, False)
        conv = await _conversacion(b, "quiero comprar")
        guion = _Guion([("ver_opciones_m", {"correo": "no-es-correo"}), ("crear_pedido_m", {}), "Hecho."])
        r = await _turno(conv, guion, monkeypatch, "quiero comprar")
        assert r is not None and r.text == "Hecho."
        vistos[runtime] = guion
        if runtime == "v3":
            sombra_conv = conv
    await _banderas(b, "legacy", False)
    legacy, sombra = vistos["legacy"], vistos["v3"]
    assert legacy.tools == sombra.tools  # mismas acciones en cada vuelta
    assert [blq[:-1] for blq in legacy.system] == [blq[:-1] for blq in sombra.system]
    assert all("Estado de la conversación" not in blq[-1] for blq in sombra.system)  # sin tarjeta
    assert LLAMADAS.count("crear_pedido_m") == 2  # en sombra la acción corre igual
    async with _scope(b) as s:
        filas = (await s.execute(text(
            "select input from audit_log where conversation_id = :c and actor = 'core:motor_sombra'"
        ), {"c": sombra_conv.id})).scalars().all()
        tareas = (await s.execute(text("select count(*) from conversation_tasks where conversation_id = :c"),
                                  {"c": sombra_conv.id})).scalar_one()
    codigos: dict[str, int] = {}
    for fila in filas:
        for k, v in fila["codigos"].items():
            codigos[k] = codigos.get(k, 0) + v
    assert codigos.get("inicio") == 1 and codigos.get("dato_invalido") == 1 and codigos.get("accion_fuera_de_etapa") == 1
    assert "no-es-correo" not in json.dumps(filas)
    assert tareas == 0  # la sombra no escribe tareas
