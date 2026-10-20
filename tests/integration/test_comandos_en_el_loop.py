"""Comandos del hilo y verificador sobre el estado contra el stack real (HU-AOS-06).

Gherkin de la HU:
- **Comandos tipados:** `iniciar_procedimiento` con un id fuera de los candidatos se rechaza sin
  crear la tarea.
- **Resultado con tarjeta:** un comando aplicado vuelve al modelo con la tarjeta actualizada y
  las acciones permitidas de la nueva etapa (y la vuelta siguiente ofrece esas acciones).
- **Verificación con estado:** con una tarea con `pedido_id`, "tu pedido OC-0042 quedó creado"
  pasa y otro número falla (reintento correctivo).

Más: con la bandera apagada (y en sombra) no aparece ningún comando; un turno que solo aplica
un comando cuenta como resuelto. Modelo guionado (sin LLM real), pack de prueba registrado en el
host del proceso.
"""

from __future__ import annotations

import json
import os
import re
import types
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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

PACK = "pack_prueba_comandos"
PROC_ID = f"{PACK}.compra"
COMANDOS = {"iniciar_procedimiento", "actualizar_tarea", "confirmar_tarea", "cancelar_tarea"}

PROCEDIMIENTO = {
    "id": PROC_ID,
    "version": "1.0.0",
    "title": "Compra de prueba",
    "triggers": {"description": "El cliente quiere comprar un plan"},
    "slots": {
        "plan": {"enum": ["basico", "premium"], "required": True},
        "pedido_id": {"type": "string", "setBy": "action:crear_pedido_c"},
        # No los escribe el modelo: un archivo lo llena el contenido del cliente y una opción con
        # candidatos se elige con un botón o una lista (revisión de seguridad).
        "foto": {"type": "media"},
        "opcion": {"type": "string", "candidates": {"source": "action:ver_planes_c.planes"}},
    },
    "stages": [
        {"id": "elegir", "actions": ["ver_planes_c"], "exitWhen": {"slots": ["plan"]}},
        {"id": "pagar", "actions": ["crear_pedido_c"], "exitWhen": {"slots": ["pedido_id"]}},
        {"id": "entregado", "actions": []},
    ],
    "completion": {"events": ["core.payment.confirmed"]},
    "claims": [{"kind": "status", "phrases": ["quedó creado"], "requires": "slot:pedido_id"}],
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


def _herramienta(nombre: str, resultado: dict[str, Any]) -> Any:
    from core.mcp.types import MCPTool

    async def handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return dict(resultado)

    return MCPTool(name=nombre, description=f"prueba {nombre}", parameters={"type": "object", "properties": {}}, handler=handler)


@pytest.fixture(scope="module")
def pack_de_prueba() -> Any:
    from agentcore_contracts import Procedure

    from core.mcp.host import mcp_host
    from core.mcp.types import Afirmacion, BaseSkill

    class _Skill(BaseSkill):
        name = "prueba_comandos"

        def get_tools(self) -> list[Any]:
            return [
                _herramienta("ver_planes_c", {"planes": ["basico", "premium"]}),
                _herramienta("crear_pedido_c", {"pedido_id": "OC-0042"}),
            ]

    async def afirmaciones() -> list[Afirmacion]:
        patron = re.compile(r"pedido\b(?:\W+[\w-]+){0,3}\W+(?:qued[óo]\s+)?cread\w*", re.IGNORECASE)
        return [Afirmacion(label="pedido_creado", patron=patron, tools=frozenset({"crear_pedido_c"}))]

    mcp_host.begin_activation(PACK)
    mcp_host.register_skill(_Skill())
    mcp_host.register_procedures([Procedure.model_validate(PROCEDIMIENTO)])
    mcp_host.register_afirmaciones(afirmaciones)
    mcp_host.end_activation()
    yield
    mcp_host.remove_plugin(PACK)


@pytest.fixture(autouse=True)
def _habilitar(pack_de_prueba: Any, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.kernel.procedures import registro

    monkeypatch.setattr(registro, "_valido", lambda proc: True)  # sus acciones no están en el catálogo del binario
    monkeypatch.setattr("core.orchestrator.loop.enabled_packs", lambda spec: {"core", PACK})


@pytest.fixture(scope="module")
async def negocio() -> AsyncIterator[uuid.UUID]:
    from core.alta_negocio import onboard_tenant
    from core.tenants import deactivate_tenant, purge_tenant

    slug = f"comandos-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Comandos {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    yield tid
    await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _conversacion(tid: uuid.UUID, mensaje: str = "hola") -> Any:
    from core.db.models import Conversation, Customer, Message

    async with _scope(tid) as s:
        cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"cmd:{uuid.uuid4().hex[:8]}")
        s.add(cliente)
        await s.flush()
        conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel="whatsapp")
        s.add(conv)
        await s.flush()
        s.add(Message(tenant_id=tid, conversation_id=conv.id, direction="inbound", sender_type="customer",
                      content=mensaje, created_at=datetime.now(UTC)))
        return types.SimpleNamespace(id=conv.id, tenant_id=tid, customer_id=cliente.id, channel="whatsapp")


async def _tarea_con_pedido(conv: Any) -> uuid.UUID:
    from core.kernel import state

    async with _scope(conv.tenant_id) as s:
        fila = await state.iniciar(
            s, tenant_id=conv.tenant_id, conversation_id=conv.id, procedure_id=PROC_ID, procedure_version="1.0.0",
            stage="entregado", actor="kernel",
        )
        for nombre, valor, fuente in (("plan", "premium", "llm"), ("pedido_id", "OC-0042", "action:crear_pedido_c")):
            await state.registrar(
                s, tenant_id=conv.tenant_id, conversation_id=conv.id, task_id=fila.id, tipo="slot_set",
                data={"slot": nombre, "value": valor, "source": fuente, "data_class": "public"}, actor="kernel",
            )
        return fila.id


async def _tareas(conv: Any) -> list[Any]:
    async with _scope(conv.tenant_id) as s:
        return list((await s.execute(text(
            "select procedure_id, stage, status, slots from conversation_tasks where conversation_id = :c"
        ), {"c": conv.id})).all())


class _Guion:
    """Modelo guionado: devuelve las respuestas en orden y guarda lo que vio."""

    provider_name = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Any]) -> None:
        self.pasos = list(pasos)
        self.system: list[list[str]] = []
        self.tools: list[list[str]] = []
        self.schemas: list[dict[str, Any]] = []
        self.mensajes: list[list[Any]] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        self.system.append(list(kwargs["system_blocks"]))
        herramientas = kwargs.get("tools") or []
        self.tools.append(sorted(t.name for t in herramientas))
        self.schemas.append({t.name: t for t in herramientas})
        self.mensajes.append(list(kwargs["messages"]))
        paso = self.pasos.pop(0) if self.pasos else "Listo."
        usage = types.SimpleNamespace(input_tokens=5, output_tokens=5)
        if isinstance(paso, tuple | list):
            from core.llm.types import LLMToolCall

            llamadas = [paso] if isinstance(paso, tuple) else paso
            return types.SimpleNamespace(
                content="", stop_reason="tool_use", model="fake", latency_ms=1, usage=usage,
                tool_calls=[
                    LLMToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=nombre, arguments=args)
                    for nombre, args in llamadas
                ],
            )
        return types.SimpleNamespace(content=paso, stop_reason="end_turn", tool_calls=[], model="fake", latency_ms=1, usage=usage)


async def _turno(conv: Any, guion: _Guion, monkeypatch, texto: str = "hola") -> Any:  # type: ignore[no-untyped-def]
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    async with tenant_scope(conv.tenant_id):
        return await process_conversation(conv.id, texto)


def _resultado(guion: _Guion, llamada: int) -> dict[str, Any]:
    """El último resultado de tool que vio el modelo en la llamada N, como JSON."""
    return json.loads(next(m.content for m in reversed(guion.mensajes[llamada]) if m.role == "tool"))


# ---------------------------------------------------------------- Gherkin


async def test_iniciar_con_un_id_fuera_de_los_candidatos_no_crea_la_tarea(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    await _banderas(negocio, "v3", True)
    try:
        conv = await _conversacion(negocio, "quiero comprar")
        guion = _Guion([
            ("iniciar_procedimiento", {"procedure_id": "pack_otro.robar_datos"}),
            "¿Qué plan te interesa?",
        ])
        resultado = await _turno(conv, guion, monkeypatch, "quiero comprar")
    finally:
        await _banderas(negocio, "legacy", False)
    assert "iniciar_procedimiento" in guion.tools[0] and "actualizar_tarea" not in guion.tools[0]
    esquema = guion.schemas[0]["iniciar_procedimiento"].parameters
    assert esquema["properties"]["procedure_id"]["enum"] == [PROC_ID]
    r = _resultado(guion, 1)
    assert r["ok"] is False and "no está disponible" in r["errors"][0]
    assert await _tareas(conv) == []
    assert resultado is not None and resultado.text == "¿Qué plan te interesa?"


async def test_el_comando_vuelve_con_la_tarjeta_y_las_acciones_de_la_nueva_etapa(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    await _banderas(negocio, "v3", True)
    try:
        conv = await _conversacion(negocio, "quiero el premium")
        guion = _Guion([
            ("iniciar_procedimiento", {"procedure_id": PROC_ID}),
            ("actualizar_tarea", {"slots": {"plan": "premium"}}),
            "Perfecto, el plan premium. Te creo el pedido.",
        ])
        resultado = await _turno(conv, guion, monkeypatch, "quiero el premium")
    finally:
        await _banderas(negocio, "legacy", False)
    iniciado = _resultado(guion, 1)
    assert iniciado["ok"] is True and iniciado["allowed_actions"] == ["ver_planes_c"]
    assert "1/3 elegir" in iniciado["state_card"] and "plan: FALTA" in iniciado["state_card"]
    # La vuelta siguiente ya ofrece las acciones de la etapa y `actualizar_tarea`, no `iniciar`.
    assert "ver_planes_c" in guion.tools[1] and "actualizar_tarea" in guion.tools[1]
    assert "iniciar_procedimiento" not in guion.tools[1]
    slots = guion.schemas[1]["actualizar_tarea"].parameters["properties"]["slots"]
    assert set(slots["properties"]) == {"plan"} and slots["additionalProperties"] is False

    actualizado = _resultado(guion, 2)
    assert actualizado == {
        "ok": True,
        "state_card": actualizado["state_card"],
        "errors": [],
        "allowed_actions": ["crear_pedido_c"],
    }
    assert "2/3 pagar" in actualizado["state_card"] and '"premium"' in actualizado["state_card"]
    assert "crear_pedido_c" in guion.tools[2] and "ver_planes_c" not in guion.tools[2]
    [(proc, stage, status, datos)] = await _tareas(conv)
    assert (proc, stage, status, datos["plan"]["value"]) == (PROC_ID, "pagar", "active", "premium")
    assert resultado is not None and resultado.text.startswith("Perfecto")

    # Los comandos no son evidencia ni `tool:*`, pero el turno quedó resuelto.
    async with _scope(negocio) as s:
        actores = (await s.execute(text(
            "select actor, action, output from audit_log where conversation_id = :c and actor like '%comando%' order by created_at"
        ), {"c": conv.id})).all()
        estado = (await s.execute(text("select state from conversations where id = :c"), {"c": conv.id})).scalar_one()
    assert [(a, b) for a, b, _o in actores] == [
        ("core:comando", "iniciar_procedimiento"), ("core:comando", "actualizar_tarea"),
    ]
    assert all(set(o) == {"ok", "errors", "codigos"} for _a, _b, o in actores)  # sin valores del cliente
    assert all(all(isinstance(c, str) and c.isidentifier() for c in o["codigos"]) for _a, _b, o in actores)
    assert int((estado or {}).get("turnos_sin_resolver", 0)) == 0


async def test_con_la_bandera_apagada_o_en_sombra_no_hay_comandos(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for runtime in ("legacy", "v3"):  # v3 sin procedimientos = modo sombra del motor
        await _banderas(negocio, runtime, False)
        try:
            conv = await _conversacion(negocio, "quiero comprar")
            guion = _Guion(["Hola, ¿qué plan te interesa?"])
            await _turno(conv, guion, monkeypatch, "quiero comprar")
        finally:
            await _banderas(negocio, "legacy", False)
        assert not set(guion.tools[0]) & COMANDOS, runtime
        assert "Estado de la conversación" not in guion.system[0][-1]


async def test_el_verificador_usa_el_pedido_de_la_tarea(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    await _banderas(negocio, "v3", True)
    try:
        conv = await _conversacion(negocio, "¿ya quedó mi pedido?")
        await _tarea_con_pedido(conv)
        guion = _Guion(["¡Sí! Tu pedido OC-0099 quedó creado.", "¡Sí! Tu pedido OC-0042 quedó creado."])
        resultado = await _turno(conv, guion, monkeypatch, "¿ya quedó mi pedido?")
    finally:
        await _banderas(negocio, "legacy", False)
    assert resultado is not None and resultado.text == "¡Sí! Tu pedido OC-0042 quedó creado."
    reintento = guion.mensajes[1][-1]
    assert reintento.role == "user" and "número de pedido o referencia" in reintento.content
    async with _scope(negocio) as s:
        veredicto = (await s.execute(text(
            "select input from audit_log where conversation_id = :c and actor = 'core:verificador'"
        ), {"c": conv.id})).scalar_one()
    assert veredicto["resultado"] != "fallo" and veredicto["motivos"] == ["referencia_sin_evidencia"]


async def test_dos_comandos_en_el_mismo_lote(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`iniciar` + `actualizar` en una misma respuesta: el segundo se decide con el estado que dejó
    el primero (no con la oferta del inicio de la vuelta), y la vuelta siguiente ya ve la etapa
    nueva. Un comando que no aplica al estado vigente se rechaza sin aplicarse."""
    await _banderas(negocio, "v3", True)
    try:
        conv = await _conversacion(negocio, "quiero el básico")
        guion = _Guion([
            [
                ("iniciar_procedimiento", {"procedure_id": PROC_ID}),
                ("actualizar_tarea", {"slots": {"plan": "basico"}}),
                ("confirmar_tarea", {}),
            ],
            "Listo, el plan básico.",
        ])
        await _turno(conv, guion, monkeypatch, "quiero el básico")
    finally:
        await _banderas(negocio, "legacy", False)
    resultados = [json.loads(m.content) for m in guion.mensajes[1] if m.role == "tool"]
    assert [r["ok"] for r in resultados] == [True, True, False]
    assert resultados[1]["allowed_actions"] == ["crear_pedido_c"]
    assert "no aplica al estado actual" in resultados[2]["errors"][0]  # nada pendiente de confirmar
    assert "crear_pedido_c" in guion.tools[1]
    [(_proc, stage, _status, datos)] = await _tareas(conv)
    assert stage == "pagar" and datos["plan"]["value"] == "basico"


# ---------------------------------------------------------------- aislamiento entre negocios


@pytest.fixture(scope="module")
async def otro_negocio() -> AsyncIterator[uuid.UUID]:
    from core.alta_negocio import onboard_tenant
    from core.tenants import deactivate_tenant, purge_tenant

    slug = f"comandos-b-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Comandos B {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    yield tid
    await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def test_el_estado_de_otro_negocio_no_es_evidencia(  # type: ignore[no-untyped-def]
    negocio: uuid.UUID, otro_negocio: uuid.UUID, monkeypatch
) -> None:
    """El pedido OC-0042 de una tarea del negocio B no respalda "tu pedido OC-0042 quedó creado"
    en una conversación del negocio A, ni se ve desde A aunque se pida por su id."""
    from core.kernel.procedures.ejecucion import instantaneas_de_tareas

    await _banderas(negocio, "v3", True)
    await _banderas(otro_negocio, "v3", True)
    try:
        conv_b = await _conversacion(otro_negocio, "¿ya quedó mi pedido?")
        await _tarea_con_pedido(conv_b)
        conv_a = await _conversacion(negocio, "¿ya quedó mi pedido?")
        guion = _Guion(["¡Sí! Tu pedido OC-0042 quedó creado."] * 3)
        resultado = await _turno(conv_a, guion, monkeypatch, "¿ya quedó mi pedido?")
        async with _scope(negocio) as s:
            desde_a = await instantaneas_de_tareas(s, negocio, conv_b.id)
            cruzado = await instantaneas_de_tareas(s, otro_negocio, conv_b.id)  # RLS: el scope es A
    finally:
        await _banderas(negocio, "legacy", False)
        await _banderas(otro_negocio, "legacy", False)
    assert desde_a == [] and cruzado == []
    # A no tiene tareas: su tarjeta es solo la pista de iniciar (adenda 2026-10-12), sin la de B.
    tarjeta_a = guion.system[0][-1].split("## Estado de la conversación", 1)[1].split("\n## ", 1)[0]
    assert "En foco" not in tarjeta_a and "OC-0042" not in tarjeta_a and "pack_prueba_comandos" not in tarjeta_a
    assert resultado is not None and resultado.text != "¡Sí! Tu pedido OC-0042 quedó creado."
    async with _scope(negocio) as s:
        veredicto = (await s.execute(text(
            "select input from audit_log where conversation_id = :c and actor = 'core:verificador'"
        ), {"c": conv_a.id})).scalar_one()
    assert "status_sin_evidencia" in veredicto["motivos"]


async def test_un_negocio_sin_el_pack_no_puede_iniciar_su_procedimiento(  # type: ignore[no-untyped-def]
    negocio: uuid.UUID, otro_negocio: uuid.UUID, monkeypatch
) -> None:
    """Los candidatos salen de los packs habilitados del negocio: B no tiene el pack de prueba,
    así que no recibe `iniciar_procedimiento` y, si el modelo lo llama igual, no se crea la tarea."""
    from core.shared.tenant import get_current_tenant_id

    monkeypatch.setattr(
        "core.orchestrator.loop.enabled_packs",
        lambda spec: {"core", PACK} if get_current_tenant_id() == negocio else {"core"},
    )
    await _banderas(otro_negocio, "v3", True)
    try:
        conv = await _conversacion(otro_negocio, "quiero comprar")
        guion = _Guion([("iniciar_procedimiento", {"procedure_id": PROC_ID}), "¿En qué te ayudo?"])
        await _turno(conv, guion, monkeypatch, "quiero comprar")
    finally:
        await _banderas(otro_negocio, "legacy", False)
    assert not set(guion.tools[0]) & COMANDOS
    r = _resultado(guion, 1)
    assert r["ok"] is False and "no aplica al estado actual" in r["errors"][0]
    assert await _tareas(conv) == []


async def test_el_motor_rechaza_los_datos_que_no_escribe_el_modelo(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Aunque el modelo los mande fuera del schema, el motor no guarda un archivo, una opción con
    candidatos ni un dato del servidor escritos por el LLM (todo o nada: tampoco el plan)."""
    await _banderas(negocio, "v3", True)
    try:
        conv = await _conversacion(negocio, "quiero el premium")
        guion = _Guion([
            ("iniciar_procedimiento", {"procedure_id": PROC_ID}),
            ("actualizar_tarea", {"slots": {"plan": "premium", "foto": {"media_id": "x"}, "opcion": "premium",
                                            "pedido_id": "OC-0001"}}),
            "Listo.",
        ])
        await _turno(conv, guion, monkeypatch, "quiero el premium")
    finally:
        await _banderas(negocio, "legacy", False)
    r = _resultado(guion, 2)
    assert r["ok"] is False and len(r["errors"]) == 3
    assert all("lo fija el sistema" in e for e in r["errors"])
    [(_proc, stage, _status, datos)] = await _tareas(conv)
    assert stage == "elegir" and datos == {}
