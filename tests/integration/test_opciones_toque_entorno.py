"""SPEC-40 · el toque con el ENTORNO REAL: la tool corre por `_execute_tool_safely` fuera del loop (HU-OPC-03).

Prueba el punto más frágil del ADR-0172: fuera de un turno no hay release ni spec. El entorno del toque los
resuelve como un turno (`load_active_spec` → `fijar_release_del_turno` → `spec_del_turno` bajo la ranura de la
release) y la tool corre con sus bindings reales, en legacy y con `aos_runtime = 'v3'`. También el camino
completo del consumer: bandera apagada = comportamiento de siempre; encendida = toque → respuesta fija con el LLM
invocado cero veces.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
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
from _api_helpers import uniq
from sqlalchemy import text
from test_opciones_toque import (
    PARAMETROS,
    TOOL,
    Envio,
    Salida,
    conversacion,
    crear_envio,
    estado,
)

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


# ---------------------------------------------------------------- la tool de prueba
class Herramienta:
    """Una tool registrada en el host: cuenta las ejecuciones y guarda el `ToolContext` que recibió."""

    def __init__(self) -> None:
        self.llamadas: list[tuple[dict[str, Any], Any]] = []

    async def handler(self, arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        self.llamadas.append((dict(arguments), context))
        return {"estado": "en camino"}


def _registrar(monkeypatch: pytest.MonkeyPatch) -> Herramienta:
    from core.mcp.host import mcp_host
    from core.mcp.types import MCPTool

    h = Herramienta()
    tool = MCPTool(name=TOOL, description="Consulta de prueba.", parameters=PARAMETROS, handler=h.handler,
                   plugin_id="core", apta_para_opcion=True)
    monkeypatch.setitem(mcp_host._tools, TOOL, tool)
    h.tool = tool  # type: ignore[attr-defined]
    return h


@pytest.fixture
def herramienta(monkeypatch):  # type: ignore[no-untyped-def]
    return _registrar(monkeypatch)


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            r = await session.execute(text(sql), params)
            return list(r.all()) if getattr(r, "returns_rows", False) else []


async def _binding(tid: uuid.UUID, *, enabled: bool = True, confirmar: bool = False, ninguno: bool = False) -> None:
    from sqlalchemy import select

    from core.agents.spec import load_active_spec
    from core.db.models import AgentSpecRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        spec = await load_active_spec()
        datos = spec.model_dump(mode="json")
        datos["tools"] = [] if ninguno else [
            {"tool": TOOL, "enabled": enabled, "scopes": [], "maxCallsPerConversation": None, "requiresConfirmation": confirmar}
        ]
        async with get_session() as s:
            fila = (await s.execute(select(AgentSpecRecord).where(AgentSpecRecord.tenant_id == tid, AgentSpecRecord.is_active.is_(True)))).scalar_one()
            fila.spec = datos


async def _objetos(tid: uuid.UUID, conv: uuid.UUID, cliente: uuid.UUID) -> tuple[Any, Any]:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            c = await s.get(Conversation, conv)
            u = await s.get(Customer, cliente)
            s.expunge_all()
            return c, u


async def tocar_real(envio: Envio, payload: str, conversation: Any, customer: Any, salida: Salida | None = None) -> tuple[str, Salida]:
    """El resolver con el entorno REAL del orquestador y el envío capturado."""
    from core.kernel.opciones.resolver import Bordes, Toque, atender_toque
    from core.orchestrator.opciones_toque import abrir_entorno
    from core.shared.tenant import tenant_scope

    salida = salida or Salida()
    async with tenant_scope(envio.tid):
        outcome = await atender_toque(
            Toque(tenant_id=envio.tid, conversation_id=envio.conv, payload=payload, wamid=uniq("wamid")),
            Bordes(abrir_entorno=lambda: abrir_entorno(conversation, customer), enviar_texto=salida.texto,
                   enviar_confirmacion=salida.confirmacion),
        )
    return outcome, salida


@pytest.fixture(scope="module")
async def legacy() -> AsyncIterator[tuple[uuid.UUID, str]]:
    from core.alta_negocio import onboard_tenant
    from core.tenants import deactivate_tenant, purge_tenant

    slug = f"opte-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Opciones {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    yield tid, slug
    await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


# ---------------------------------------------------------------- legacy: bindings reales
async def test_la_tool_corre_por_el_loop_con_el_binding_real_y_la_idempotencia_de_la_activacion(legacy, herramienta) -> None:
    tid, _ = legacy
    await _binding(tid)
    conv_id, cliente = await conversacion(tid)
    conv, cust = await _objetos(tid, conv_id, cliente)
    envio = await crear_envio(tid, conv_id)

    outcome, salida = await tocar_real(envio, envio.payload(), conv, cust)

    assert outcome == "ok" and salida.textos == ["Ana, tu pedido está en camino."]
    [(args, contexto)] = herramienta.llamadas
    assert args == {"pedido": "A-1"}
    [(_, estado_, _)] = await estado(envio)
    assert estado_ == "done"
    [(activacion,)] = await _sql(tid, "select id from option_activations where send_id = :s", s=envio.send_id)
    assert contexto.idempotency_key == f"{conv_id}:{activacion}", "idempotency_key = conversación:activation_id"
    ejecutadas = await _sql(tid, "select input from audit_log where actor = :a and action = 'execute' and conversation_id = :c", a=f"tool:{TOOL}", c=conv_id)
    assert len(ejecutadas) == 1, "el loop audita la ejecución como cualquier tool"
    assert ejecutadas[0][0] == {"keys": ["pedido"], "args_hash": ejecutadas[0][0]["args_hash"]} and "A-1" not in str(ejecutadas[0][0]), \
        "la auditoría de la tool de un toque no copia los valores (vienen del contexto del cliente)"


async def test_sin_binding_explicito_la_tool_no_corre_aunque_el_loop_la_dejaria(legacy, herramienta) -> None:
    from core.agents.spec import load_active_spec
    from core.orchestrator.loop import _tool_denial
    from core.shared.tenant import tenant_scope

    tid, _ = legacy
    await _binding(tid, ninguno=True)
    conv_id, cliente = await conversacion(tid)
    conv, cust = await _objetos(tid, conv_id, cliente)
    envio = await crear_envio(tid, conv_id)
    async with tenant_scope(tid):
        assert await _tool_denial(await load_active_spec(), TOOL, conv_id) is None, "el fail-open transitorio del loop"

    outcome, salida = await tocar_real(envio, envio.payload(), conv, cust)

    assert outcome == "denied" and herramienta.llamadas == [] and salida.textos == ["No pude consultarlo, Ana."]
    [fila] = await _sql(tid, "select output from audit_log where action = 'option_tap' and input->>'send_id' = :s", s=str(envio.send_id))
    assert "tool_sin_binding" in fila[0]["codigo"]


async def test_binding_deshabilitado_o_tool_no_apta_o_pack_inactivo_no_corren(legacy, herramienta) -> None:
    tid, _ = legacy
    conv_id, cliente = await conversacion(tid)
    conv, cust = await _objetos(tid, conv_id, cliente)

    await _binding(tid, enabled=False)
    envio = await crear_envio(tid, conv_id)
    assert (await tocar_real(envio, envio.payload(), conv, cust))[0] == "denied"

    await _binding(tid)
    herramienta.tool.apta_para_opcion = False
    envio = await crear_envio(tid, conv_id)
    assert (await tocar_real(envio, envio.payload(), conv, cust))[0] == "denied"
    herramienta.tool.apta_para_opcion = True

    herramienta.tool.plugin_id = "pack_que_el_negocio_no_tiene"
    envio = await crear_envio(tid, conv_id)
    assert (await tocar_real(envio, envio.payload(), conv, cust))[0] == "denied"
    herramienta.tool.plugin_id = "core"

    assert herramienta.llamadas == []


async def test_tool_gobernada_por_un_procedimiento_no_corre(legacy, herramienta, monkeypatch) -> None:
    tid, _ = legacy
    await _binding(tid)
    etapa = types.SimpleNamespace(actions=[TOOL], preconditions=[], confirmActions=[])
    proc = types.SimpleNamespace(stages=[etapa])
    monkeypatch.setattr("core.kernel.procedures.registro.disponibles", lambda *_a, **_k: {"p": proc})
    conv_id, cliente = await conversacion(tid)
    conv, cust = await _objetos(tid, conv_id, cliente)
    envio = await crear_envio(tid, conv_id)

    outcome, _ = await tocar_real(envio, envio.payload(), conv, cust)

    assert outcome == "denied" and herramienta.llamadas == []


async def test_binding_con_requires_confirmation_confirma_con_optc_y_ejecuta_sin_el_pendiente_del_loop(legacy, herramienta) -> None:
    from core.agents.spec import load_active_spec
    from core.llm.types import LLMToolCall
    from core.orchestrator.loop import _execute_tool_safely
    from core.shared.tenant import tenant_scope

    tid, _ = legacy
    await _binding(tid, confirmar=True)
    conv_id, cliente = await conversacion(tid)
    conv, cust = await _objetos(tid, conv_id, cliente)
    envio = await crear_envio(tid, conv_id)

    outcome, salida = await tocar_real(envio, envio.payload(), conv, cust)
    assert outcome == "confirm_requested" and herramienta.llamadas == []
    optc = salida.confirmaciones[0]["buttons"][0]["id"]

    outcome, _ = await tocar_real(envio, optc, conv, cust)
    assert outcome == "ok" and len(herramienta.llamadas) == 1
    assert "pending_confirmation" not in (conv.state or {}), "el loop no deja una confirmación suya pendiente"

    # sin `ya_confirmada`, el mismo binding frena a una llamada normal (es lo que el parámetro salta)
    async with tenant_scope(tid):
        spec = await load_active_spec()
        frenada = await _execute_tool_safely(conv, cust, LLMToolCall(id="x", name=TOOL, arguments={"pedido": "A-1"}), spec)
    assert frenada["status"] == "confirmation_required" and len(herramienta.llamadas) == 1


# ---------------------------------------------------------------- v3: la release y los bindings por agente
async def _banderas(tid: uuid.UUID, runtime: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("update tenants set aos_runtime = :r where id = :t"), {"r": runtime, "t": tid})
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def v3() -> AsyncIterator[tuple[tuple[uuid.UUID, uuid.UUID], tuple[uuid.UUID, uuid.UUID]]]:
    """Dos negocios en v3 con su release 1: el A con el binding de la tool habilitado en el General, el B con él deshabilitado.

    (Un binding AUSENTE no sirve de caso negativo: la conversión a v3 crea uno por cada acción ofrecida sin
    binding —el consentimiento del lector tolerante— y la release ya lo trae explícito.)"""
    from core.alta_negocio import onboard_tenant
    from core.config import get_settings
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs
    from core.releases.release_uno import backfill_releases
    from core.tenants import deactivate_tenant, purge_tenant

    await journeys._preparar_proceso()
    assert not (await sync_packs(plugin_manager.scan())).fallo
    settings = get_settings()
    antes = settings.aos_connection_routing
    settings.aos_connection_routing = True  # v3 lo exige (ADR-0112)
    creados: list[tuple[uuid.UUID, str]] = []
    salida: list[tuple[uuid.UUID, uuid.UUID]] = []
    registro = pytest.MonkeyPatch()
    h = _registrar(registro)
    # El backfill descarta los bindings de acciones que no existen en el binario: el catálogo sale de las
    # skills declaradas, así que se le suma una skill de prueba que ofrece la tool (y se reinicia su caché).
    skill = types.SimpleNamespace(get_tools=lambda: [h.tool])  # type: ignore[attr-defined]
    from core.kernel.procedures import catalogo as _catalogo

    originales = _catalogo._core_skills
    registro.setattr(_catalogo, "_core_skills", lambda: [*originales(), skill])
    registro.setattr(_catalogo, "_cache", None)
    for con_binding in (True, False):
        slug = f"optv3-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Opciones {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
        creados.append((tid, slug))
        await _binding(tid, enabled=con_binding)
        cid = uuid.uuid4()
        await _sql(
            tid,
            "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
            "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
            id=cid, t=tid, ext=f"ig-{cid.hex[:8]}",
        )
        reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=plugin_manager.scan())
        assert not reporte.fallo, reporte.errores
        await _banderas(tid, "v3")
        salida.append((tid, cid))
    registro.undo()
    yield salida[0], salida[1]
    settings.aos_connection_routing = antes
    for tid, slug in creados:
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _conversacion_v3(tid: uuid.UUID, cid: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            c = Customer(tenant_id=tid, channel="whatsapp", external_id=f"v3:{uuid.uuid4().hex[:8]}")
            s.add(c)
            await s.flush()
            conv = Conversation(tenant_id=tid, customer_id=c.id, channel="whatsapp", connection_id=cid)
            s.add(conv)
            await s.flush()
            return conv.id, c.id


async def test_v3_la_tool_corre_con_la_release_y_los_bindings_del_agente_en_foco(v3, herramienta) -> None:
    (tid, cid), _ = v3
    conv_id, cliente = await _conversacion_v3(tid, cid)
    conv, cust = await _objetos(tid, conv_id, cliente)
    envio = await crear_envio(tid, conv_id)

    outcome, salida = await tocar_real(envio, envio.payload(), conv, cust)

    assert outcome == "ok" and salida.textos == ["Ana, tu pedido está en camino."]
    [(args, contexto)] = herramienta.llamadas
    assert args == {"pedido": "A-1"}
    # Prueba de que el contexto de turno SÍ existe fuera del loop: la identidad del agente en foco y su
    # `memory.writeNamespaces` salen de la release (None en legacy), y la conversación quedó asignada.
    assert contexto.agent_id is not None and contexto.agent_version_id is not None
    assert contexto.write_namespaces is not None
    [(release, dueno)] = await _sql(tid, "select release_id, active_agent_instance_id from conversations where id = :c", c=conv_id)
    assert release is not None and dueno is not None


async def test_v3_binding_deshabilitado_en_el_agente_en_foco_no_corre(v3, herramienta) -> None:
    _, (tid, cid) = v3
    conv_id, cliente = await _conversacion_v3(tid, cid)
    conv, cust = await _objetos(tid, conv_id, cliente)
    envio = await crear_envio(tid, conv_id)

    outcome, _ = await tocar_real(envio, envio.payload(), conv, cust)

    assert outcome == "denied" and herramienta.llamadas == []


async def test_la_ranura_de_la_release_se_cierra_al_terminar_el_toque(v3, herramienta) -> None:
    from core.shared import release_vigente

    (tid, cid), _ = v3
    conv_id, cliente = await _conversacion_v3(tid, cid)
    conv, cust = await _objetos(tid, conv_id, cliente)
    envio = await crear_envio(tid, conv_id)

    await tocar_real(envio, envio.payload(), conv, cust)

    assert release_vigente.vigente() is None, "lo que fija un toque no lo ve el siguiente turno"


async def test_el_toque_no_corre_el_router_ni_consume_el_payload_de_boton_del_turno_diferido(v3, herramienta) -> None:
    from core.kernel.router import guardar_payload, tomar_payload
    from core.shared.tenant import tenant_scope

    (tid, cid), _ = v3
    conv_id, cliente = await _conversacion_v3(tid, cid)
    conv, cust = await _objetos(tid, conv_id, cliente)
    envio = await crear_envio(tid, conv_id)
    from _api_helpers import banderas_como_dueno

    await banderas_como_dueno(tid, aos_routing="shadow")
    try:
        async with tenant_scope(tid):
            await guardar_payload(tid, conv_id, "proc:pagar")  # lo espera el turno diferido del botón de un procedimiento
        outcome, _ = await tocar_real(envio, envio.payload(), conv, cust)
        async with tenant_scope(tid):
            restante = await tomar_payload(tid, conv_id)
    finally:
        await banderas_como_dueno(tid, aos_routing="off")

    assert outcome == "ok"
    assert restante == "proc:pagar", "el toque no consume (GETDEL) el payload de ruteo del turno que espera"


# ---------------------------------------------------------------- el consumer: bandera y journey sin LLM
class _LlmProhibido:
    provider_name = "prohibido"
    model = "prohibido"
    pricing_per_mtok = (0.0, 0.0)
    llamadas = 0

    def estimate_cost_usd(self, *_a: Any) -> float:
        return 0.0

    async def generate(self, **_kwargs: Any):
        type(self).llamadas += 1
        raise AssertionError("el toque de una opción no puede invocar al LLM")


def _inbound(tid: uuid.UUID, cliente: str, contenido: str, interactivo: str = "") -> dict:
    return {
        "tenant_id": str(tid),
        "provider": "meta",
        "channel": "whatsapp",
        "external_message_id": uniq("wamid"),
        "customer_external_id": cliente,
        "customer_name": "Cliente",
        "content": contenido,
        "connection_external_id": "sin-conexion",
        "media": "[]",
        "media_context": "",
        **({"interactive_id": interactivo} if interactivo else {}),
    }


async def _conversacion_del_consumer(tid: uuid.UUID, cliente: str) -> uuid.UUID:
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(_inbound(tid, cliente, "hola"))
    [(conv,)] = await _sql(
        tid,
        "select cv.id from conversations cv join customers c on c.id = cv.customer_id where c.external_id = :e "
        "order by cv.started_at desc limit 1",
        e=cliente,
    )
    return conv


async def _salientes(tid: uuid.UUID, conv: uuid.UUID) -> list[str]:
    filas = await _sql(tid, "select content from messages where conversation_id = :c and direction = 'outbound' order by created_at", c=conv)
    return [f[0] for f in filas]


def _inmediato(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import DEBOUNCE_DELAY_MS

    for modo in list(DEBOUNCE_DELAY_MS):
        monkeypatch.setitem(DEBOUNCE_DELAY_MS, modo, 0)


async def _bandera(tid: uuid.UUID, encendida: bool) -> None:
    from core.kernel.opciones import bandera
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await bandera.fijar(tid, encendida, actor="test")


async def test_bandera_apagada_el_payload_opt_sigue_el_camino_de_siempre(legacy, herramienta, monkeypatch, recorded) -> None:  # type: ignore[no-untyped-def]
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    tid, _ = legacy
    await _binding(tid)
    await _bandera(tid, False)
    _inmediato(monkeypatch)
    cliente = uniq("wa-off")
    conv = await _conversacion_del_consumer(tid, cliente)
    recorded.clear()
    envio = await crear_envio(tid, conv)

    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(_inbound(tid, cliente, "Ver estado", envio.payload()))

    assert recorded == ["Ver estado"], "con la bandera apagada el mensaje llega al LLM como texto, igual que hoy"
    assert herramienta.llamadas == [] and await estado(envio) == []
    assert not any("option_tap" in str(f) for f in await _sql(tid, "select action from audit_log where conversation_id = :c", c=conv))


async def test_bandera_encendida_journey_toque_respuesta_con_el_llm_en_cero(legacy, herramienta, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    tid, _ = legacy
    await _binding(tid)
    await _bandera(tid, True)
    _inmediato(monkeypatch)
    cliente = uniq("wa-on")
    conv = await _conversacion_del_consumer(tid, cliente)
    _LlmProhibido.llamadas = 0
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: _LlmProhibido())
    antes = len(await _salientes(tid, conv))
    envio = await crear_envio(tid, conv)

    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(_inbound(tid, cliente, "Ver estado", envio.payload()))

    assert _LlmProhibido.llamadas == 0, "cero invocaciones del modelo"
    assert [a for a, _ in herramienta.llamadas] == [{"pedido": "A-1"}]
    assert (await _salientes(tid, conv))[antes:] == ["Ana, tu pedido está en camino."], "la respuesta fija, por la cola normal"
    assert await estado(envio) == [("ver", "done", None)]


async def test_toque_ajeno_con_la_bandera_encendida_no_llega_al_llm_ni_responde(legacy, herramienta, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    tid, _ = legacy
    await _bandera(tid, True)
    _inmediato(monkeypatch)
    cliente = uniq("wa-mal")
    conv = await _conversacion_del_consumer(tid, cliente)
    _LlmProhibido.llamadas = 0
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: _LlmProhibido())
    antes = len(await _salientes(tid, conv))

    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(_inbound(tid, cliente, "Ver", "opt:" + "a" * 32 + ":ver:" + "b" * 16))

    assert _LlmProhibido.llamadas == 0 and herramienta.llamadas == []
    assert len(await _salientes(tid, conv)) == antes, "un payload que no valida se descarta sin responder"


async def test_quick_reply_sin_prefijo_sigue_el_flujo_normal_con_la_bandera_encendida(legacy, monkeypatch, recorded) -> None:  # type: ignore[no-untyped-def]
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    tid, _ = legacy
    await _bandera(tid, True)
    _inmediato(monkeypatch)
    cliente = uniq("wa-qr")
    conv = await _conversacion_del_consumer(tid, cliente)
    recorded.clear()
    del conv

    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(_inbound(tid, cliente, "Sí, me interesa", "promo_otono"))

    assert recorded == ["Sí, me interesa"], "pasa por el flujo normal (texto al LLM)"
