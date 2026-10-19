"""HU-AOS-17 · enrutamiento entre especialistas con el loop real y un modelo guionado (sin LLM).

Negocio `aos_runtime = v3` con el motor de procedimientos, `pack_commerce` y `pack_calendar`, dos
especialistas publicados (Ventas por defecto con `contratar_plan`, Agenda con `agendar_cita`) y
`AOS_CONNECTION_ROUTING` encendida:

- **off** (por defecto): todo como antes: `route_source = default`, atiende Ventas, sin sombra.
- **shadow**: decide y registra (`shadow_route` con Agenda por disparador) pero sigue Ventas.
- **active**: "quiero agendar una demo" → atiende Agenda (`route_source = trigger`, confianza
  ≥ 0,80), `conversations.active_agent_instance_id` = Agenda y el hilo ve solo `agendar_cita`.
- **Tarea en foco**: con una tarea de Ventas en foco, el siguiente turno lo atiende Ventas sin
  clasificar (`task`, `focus_task_id`).
- **Regla de botón**: el payload `plan:` de la regla del KernelSpec inicia `contratar_plan` con
  Ventas como dueño (`rule`, `rule_id`, `focus_task_id`).
- **Aislamiento**: el payload y la bandera de un negocio no cambian la ruta de otro.
"""

from __future__ import annotations

import json
import os
import uuid
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

PLAN = "pack_commerce.contratar_plan"
CITA = "pack_calendar.agendar_cita"
PACKS = ["pack_commerce", "pack_calendar"]
HORAS = '{"mon": "09:00-18:00", "tue": "09:00-18:00", "wed": "09:00-18:00", "thu": "09:00-18:00", "fri": "09:00-18:00", "sat": "09:00-18:00", "sun": "09:00-18:00"}'


# ---------------------------------------------------------------- helpers
def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


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


@pytest.fixture(scope="module", autouse=True)
async def _proceso() -> None:
    """Packs registrados en `pack_versions` y activos en el proceso, y las tools del core."""
    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs
    from core.tools.core_skill import register_core_skills

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()
    await plugin_manager.ensure_process_activation()
    register_core_skills(mcp_host)


async def _negocio() -> tuple[uuid.UUID, uuid.UUID]:
    """Negocio v3 con commerce y calendar, una conexión conversacional y su release 1 (General
    por defecto, construida en legacy como el backfill). (tid, conexión)."""
    from core.alta_negocio import onboard_tenant
    from core.plugin_manager import plugin_manager
    from core.releases.release_uno import backfill_releases
    from core.shared.tenant import tenant_scope

    slug = f"aos17-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"AOS17 {slug}", slug=slug, packs=PACKS, sembrar_ejemplos=False, plan="premium")
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_calendar", {
            "source": "interno", "business_hours": HORAS, "timezone": "America/Bogota",
            "slot_minutes": 60, "buffer_minutes": 0, "event_duration_minutes": 60,
        })
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid, t=tid, ext=f"ig-{uuid.uuid4().hex[:10]}",
    )
    reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte.errores
    await _banderas(tid, "v3", True)
    return tid, cid


async def _especialista(tid: uuid.UUID, cid: uuid.UUID, key: str, proc: str, tools: set[str], *, defecto: bool) -> uuid.UUID:
    """Agente especialista publicado e instanciado en la conexión. Devuelve el id del agente."""
    from core.agents import instancias, versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    catalogo = catalogo_de_acciones(_modules())
    async with tenant_scope(tid):
        agente = await versiones.crear_agente(tid, key=key, kind="specialist", name=key.title())
        borrador = await versiones.obtener_borrador(tid, agente.id)
        contenido = dict(borrador.content)
        contenido["procedures"] = [{"id": proc, "version": "^1.0"}]
        contenido["tools"] = [{"tool": t, "enabled": True} for t in sorted(tools)]
        await versiones.guardar_borrador(tid, agente.id, contenido, borrador.lock_version, catalogo=catalogo)
        await versiones.publicar(tid, agente.id, catalogo=catalogo)
        await instancias.agregar_instancia(tid, cid, agente.id, is_default=defecto)
    return agente.id


async def _por_defecto(tid: uuid.UUID, cid: uuid.UUID, agent_id: uuid.UUID) -> None:
    from core.agents import instancias
    from core.shared.tenant import tenant_scope

    ((iid,),) = await _sql(
        tid, "select id from agent_instances where connection_id = :c and agent_id = :a and deleted_at is null",
        c=cid, a=agent_id,
    )
    async with tenant_scope(tid):
        await instancias.actualizar_instancia(tid, cid, iid, is_default=True)


async def _publicar_release(tid: uuid.UUID, cid: uuid.UUID) -> uuid.UUID:
    from core.releases.build import construir
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        build = await construir(tid, cid, actor="test", modules=_modules())
        if build.creada:
            await promover(tid, cid, build.release_id, actor="test")
    return build.release_id


async def _conversacion(tid: uuid.UUID, cid: uuid.UUID | None) -> uuid.UUID:
    cliente, conv = uuid.uuid4(), uuid.uuid4()
    await _sql(
        tid, "insert into customers (id, tenant_id, channel, external_id) values (:id, :t, 'instagram', :e)",
        id=cliente, t=tid, e=f"cli-{uuid.uuid4().hex[:10]}",
    )
    await _sql(
        tid,
        "insert into conversations (id, tenant_id, customer_id, channel, connection_id) "
        "values (:id, :t, :cli, 'instagram', :c)",
        id=conv, t=tid, cli=cliente, c=cid,
    )
    return conv


async def _mensaje(tid: uuid.UUID, conv: uuid.UUID, cuando: datetime | None = None, direccion: str = "inbound") -> None:
    await _sql(
        tid,
        "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content, created_at) "
        "values (:id, :t, :c, :d, :s, 'hola', :w)",
        id=uuid.uuid4(), t=tid, c=conv, d=direccion,
        s="customer" if direccion == "inbound" else "agent", w=cuando or datetime.now(UTC),
    )


def _enum(tool: Any) -> list[str] | None:
    """El enum de `procedure_id` de `iniciar_procedimiento` (dict o MCPTool)."""
    esquema = tool.get("parameters") or tool.get("input_schema") if isinstance(tool, dict) else (
        getattr(tool, "parameters", None) or getattr(tool, "input_schema", None)
    )
    if isinstance(esquema, dict):
        return list(((esquema.get("properties") or {}).get("procedure_id") or {}).get("enum") or [])
    return None


def _nombre(t: Any) -> str:
    return str(t.get("name") if isinstance(t, dict) else getattr(t, "name", ""))


class _Guion:
    """Modelo guionado: un paso por llamada; guarda las tools ofrecidas, el enum de
    `iniciar_procedimiento` y los resultados de tools que vio."""

    pricing_per_mtok = (0.0, 0.0)
    model = "deepseek-flash"
    provider_name = "deepseek"

    def __init__(self, pasos: list[Any]) -> None:
        self.pasos = list(pasos)
        self.tools: list[list[str]] = []
        self.enums: list[list[str] | None] = []
        self.resultados: list[str] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMResponse, LLMToolCall, LLMUsageInfo

        tools = list(kwargs.get("tools") or [])
        self.tools.append(sorted(_nombre(t) for t in tools))
        iniciar = [t for t in tools if _nombre(t) == "iniciar_procedimiento"]
        self.enums.append(sorted(_enum(iniciar[0]) or []) if iniciar else None)
        mensajes = kwargs.get("messages") or []
        if mensajes and getattr(mensajes[-1], "role", None) == "tool":
            self.resultados.append(str(mensajes[-1].content))
        paso = self.pasos.pop(0) if self.pasos else "Listo."
        uso = LLMUsageInfo(input_tokens=5, output_tokens=5)
        if isinstance(paso, tuple):
            nombre, args = paso
            return LLMResponse(content="", stop_reason="tool_use", model=self.model, usage=uso,
                               tool_calls=[LLMToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=nombre, arguments=args)])
        return LLMResponse(content=paso, stop_reason="end_turn", model=self.model, usage=uso)


async def _turno(tid: uuid.UUID, conv: uuid.UUID, monkeypatch: Any, pasos: list[Any], texto: str = "hola") -> _Guion:
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    guion = _Guion(pasos)
    await _mensaje(tid, conv)
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    async with tenant_scope(tid):
        await process_conversation(conv, texto)
    await _mensaje(tid, conv, direccion="outbound")
    return guion


async def _release_del_turno(tid: uuid.UUID, conv: uuid.UUID) -> uuid.UUID | None:
    filas = await _sql(
        tid, "select release_id from core_decisions where conversation_id = :c order by created_at desc limit 1", c=conv
    )
    return filas[0][0] if filas else None


async def _tareas(tid: uuid.UUID, conv: uuid.UUID) -> list[Any]:
    return await _sql(tid, "select procedure_id, status from conversation_tasks where conversation_id = :c", c=conv)


@pytest.fixture
def _ruteo(monkeypatch: Any) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)


async def _modo(tid: uuid.UUID, modo: str) -> None:
    from core.kernel.ruteo_sombra import escribir_modo

    await escribir_modo(tid, modo)


async def _con_dos_especialistas(modo: str, reglas: list[dict[str, Any]] | None = None) -> tuple[uuid.UUID, uuid.UUID, dict[str, uuid.UUID]]:
    """Negocio con Ventas (por defecto) y Agenda publicados y su release; `reglas` en el KernelSpec."""
    tid, cid = await _negocio()
    await _especialista(tid, cid, "ventas", PLAN, {"informar_plan", "consultar_inventario"}, defecto=True)
    await _especialista(tid, cid, "agenda", CITA, {"list_appointment_types", "check_availability"}, defecto=False)
    if reglas is not None:
        await _reglas_del_coordinador(tid, reglas)
    await _publicar_release(tid, cid)
    await _modo(tid, modo)
    filas = await _sql(
        tid,
        "select a.key, i.id from agent_instances i join agents a on a.tenant_id = i.tenant_id and a.id = i.agent_id "
        "where i.connection_id = :c and i.deleted_at is null",
        c=cid,
    )
    return tid, cid, {k: i for k, i in filas}


async def _reglas_del_coordinador(tid: uuid.UUID, reglas: list[dict[str, Any]]) -> None:
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    ((aid, contenido),) = await _sql(
        tid,
        "select a.id, v.content from agents a join agent_versions v on v.tenant_id = a.tenant_id and v.agent_id = a.id "
        "where a.key = 'coordinador' order by v.version desc limit 1",
    )
    nuevo = dict(contenido)
    nuevo["routing"] = {**(nuevo.get("routing") or {}), "rules": reglas}
    async with tenant_scope(tid):
        await versiones.publicar(tid, aid, catalogo=catalogo_de_acciones(_modules()), contenido=nuevo)


async def _decision(tid: uuid.UUID, conv: uuid.UUID) -> dict[str, Any]:
    ((fila,),) = await _sql(
        tid,
        "select row_to_json(d) from (select route_source, rule_id, candidates, confidence, shadow_route, "
        "active_instance_id, focus_task_id from core_decisions where conversation_id = :c "
        "order by created_at desc limit 1) d",
        c=conv,
    )
    return fila if isinstance(fila, dict) else json.loads(fila)


async def _dueno(tid: uuid.UUID, conv: uuid.UUID) -> uuid.UUID | None:
    ((dueno,),) = await _sql(tid, "select active_agent_instance_id from conversations where id = :c", c=conv)
    return dueno


DEMO = "quiero agendar una demo"


# ---------------------------------------------------------------- off, sombra y activo
async def test_apagado_todo_sigue_igual(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, inst = await _con_dos_especialistas("off")
    conv = await _conversacion(tid, cid)
    g = await _turno(tid, conv, monkeypatch, ["¿Qué plan te interesa?"], texto=DEMO)
    d = await _decision(tid, conv)
    assert d["route_source"] == "default"
    assert d["shadow_route"] is None and d["candidates"] is None and d["rule_id"] is None
    assert d["active_instance_id"] == str(inst["ventas"])
    assert await _dueno(tid, conv) == inst["ventas"]
    assert g.enums[0] == [PLAN]


async def test_sombra_decide_y_registra_pero_atiende_el_de_siempre(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, inst = await _con_dos_especialistas("shadow")
    conv = await _conversacion(tid, cid)
    g = await _turno(tid, conv, monkeypatch, ["¿Qué plan te interesa?"], texto=DEMO)
    d = await _decision(tid, conv)
    assert d["route_source"] == "default" and d["rule_id"] is None and d["confidence"] is None
    assert d["active_instance_id"] == str(inst["ventas"])
    sombra = d["shadow_route"]
    assert sombra["agent_key"] == "agenda" and sombra["route_source"] == "trigger" and sombra["procedure_id"] == CITA
    assert sombra["instance_id"] == str(inst["agenda"])
    assert d["candidates"][0]["procedure_id"] == CITA and d["candidates"][0]["owner"] == "agenda"
    assert await _dueno(tid, conv) == inst["ventas"]
    assert g.enums[0] == [PLAN]  # lo que ve el modelo no cambia


async def test_activo_el_disparador_pasa_el_turno_a_agenda(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, inst = await _con_dos_especialistas("active")
    conv = await _conversacion(tid, cid)
    g = await _turno(tid, conv, monkeypatch, ["¿Para qué día?"], texto=DEMO)
    d = await _decision(tid, conv)
    assert d["route_source"] == "trigger" and d["shadow_route"] is None
    assert float(d["confidence"]) >= 0.80
    assert d["active_instance_id"] == str(inst["agenda"])
    assert await _dueno(tid, conv) == inst["agenda"]
    assert g.enums[0] == [CITA]
    # Un saludo después no cambia de dueño: el de la conversación sigue (default).
    g = await _turno(tid, conv, monkeypatch, ["Hola."], texto="gracias")
    d = await _decision(tid, conv)
    assert d["route_source"] == "default" and d["active_instance_id"] == str(inst["agenda"])


async def test_activo_con_tarea_de_ventas_en_foco_atiende_ventas_sin_clasificar(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid, inst = await _con_dos_especialistas("active")
    conv = await _conversacion(tid, cid)
    await _turno(tid, conv, monkeypatch, [("iniciar_procedimiento", {"procedure_id": PLAN}), "¿Qué plan?"], texto="quiero el plan premium")
    ((task_id, dueno_tarea),) = await _sql(
        tid, "select id, owner_instance_id from conversation_tasks where conversation_id = :c and status = 'active'", c=conv
    )
    assert dueno_tarea == inst["ventas"]
    await _turno(tid, conv, monkeypatch, ["Anotado."], texto=DEMO)
    d = await _decision(tid, conv)
    assert d["route_source"] == "task" and d["focus_task_id"] == str(task_id)
    assert d["active_instance_id"] == str(inst["ventas"]) and d["candidates"] == []


async def test_activo_la_regla_del_boton_inicia_el_procedimiento(_ruteo: None, monkeypatch: Any) -> None:
    from core.kernel.procedures.ejecucion import entrada_sin_llm
    from core.shared.tenant import tenant_scope

    reglas = [{"id": "boton_plan", "when": {"interactivePayloadPrefix": "plan:"}, "start": PLAN}]
    tid, cid, inst = await _con_dos_especialistas("active", reglas)
    conv = await _conversacion(tid, cid)
    async with tenant_scope(tid):
        conversacion = type("C", (), {"tenant_id": tid, "id": conv})()
        await entrada_sin_llm(conversacion, {"interactive_id": "plan:premium"})
    await _turno(tid, conv, monkeypatch, ["Perfecto, ¿a qué correo te envío la factura?"], texto="Pagar")
    d = await _decision(tid, conv)
    assert d["route_source"] == "rule" and d["rule_id"] == "boton_plan" and float(d["confidence"]) == 1.0
    assert d["active_instance_id"] == str(inst["ventas"])
    ((task_id, proc, dueno_tarea),) = await _sql(
        tid, "select id, procedure_id, owner_instance_id from conversation_tasks where conversation_id = :c", c=conv
    )
    assert (proc, dueno_tarea) == (PLAN, inst["ventas"])
    assert d["focus_task_id"] == str(task_id)


async def test_el_build_rechaza_una_regla_a_un_agente_que_no_esta(_ruteo: None) -> None:
    from core.releases.build import ErrorDelBuild

    tid, cid = await _negocio()
    await _especialista(tid, cid, "ventas", PLAN, {"informar_plan"}, defecto=True)
    await _reglas_del_coordinador(tid, [{"id": "mora", "when": {"channel": "whatsapp"}, "agent": "cobranza"}])
    with pytest.raises(ErrorDelBuild) as error:
        await _publicar_release(tid, cid)
    assert error.value.codigo == "ruteo_invalido"


# ---------------------------------------------------------------- aislamiento y orden de banderas
async def test_la_ruta_de_un_negocio_no_cruza_a_otro(_ruteo: None, monkeypatch: Any) -> None:
    from core.kernel.router import guardar_payload

    reglas = [{"id": "boton_plan", "when": {"interactivePayloadPrefix": "plan:"}, "start": PLAN}]
    a, cid_a, _inst_a = await _con_dos_especialistas("active", reglas)
    b, cid_b, inst_b = await _con_dos_especialistas("off", reglas)
    conv_a = await _conversacion(a, cid_a)
    conv_b = await _conversacion(b, cid_b)
    # Un payload guardado para la conversación de B bajo el negocio A no le llega a B.
    await guardar_payload(a, conv_b, "plan:premium")
    await _turno(b, conv_b, monkeypatch, ["Hola."], texto="Pagar")
    d = await _decision(b, conv_b)
    assert d["route_source"] == "default" and d["active_instance_id"] == str(inst_b["ventas"])
    assert await _sql(b, "select id from conversation_tasks where conversation_id = :c", c=conv_b) == []
    # A no ve las decisiones de B (RLS + tenant_id).
    assert await _sql(a, "select id from core_decisions where conversation_id = :c", c=conv_b) == []
    del conv_a


async def test_el_ruteo_exige_v3_y_volver_a_legacy_lo_apaga(_ruteo: None) -> None:
    from core.releases.migracion_v3 import _escribir_bandera

    tid, _cid = await _negocio()
    await _modo(tid, "shadow")
    await _escribir_bandera(None, tid, runtime="legacy", procedimientos=False)
    ((runtime, ruteo),) = await _sql(None, "select aos_runtime, aos_routing from tenants where id = :t", t=tid)
    assert (runtime, ruteo) == ("legacy", "off")
    with pytest.raises(ValueError):
        await _modo(tid, "active")


# ---------------------------------------------------------------- punta a punta con los presets v2 (HU-AOS-46)
async def _negocio_con_preset_suscripciones() -> tuple[uuid.UUID, uuid.UUID, Any, uuid.UUID, dict[str, Any]]:
    """Negocio v3 con commerce, calendar y subscriptions, una conexión y `presets-v2 --aplicar`
    (su función) del preset `suscripciones`. Devuelve (tid, conexión, reporte, id y manifiesto de
    la release candidata, sin promover)."""
    from core.alta_negocio import onboard_tenant
    from core.plugin_manager import plugin_manager
    from core.presets.agentes import backfill_presets_v2
    from core.releases.release_uno import backfill_releases
    from core.shared.tenant import tenant_scope

    slug = f"aos17p-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(
        name=f"AOS17 {slug}", slug=slug, packs=["pack_commerce", "pack_calendar", "pack_subscriptions"],
        sembrar_ejemplos=False, plan="premium",
    )
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_calendar", {
            "source": "interno", "business_hours": HORAS, "timezone": "America/Bogota",
            "slot_minutes": 60, "buffer_minutes": 0, "event_duration_minutes": 60,
        })
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid, t=tid, ext=f"ig-{uuid.uuid4().hex[:10]}",
    )
    assert not (await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())).fallo
    await _banderas(tid, "v3", True)

    (reporte,) = await backfill_presets_v2(tenant_ids=[tid], preset_id="suscripciones", aplicar=True, modules=_modules())
    assert not reporte.fallo, reporte.errores
    numero = reporte.releases[str(cid)]
    assert numero.isdigit(), numero  # el build no falló (p. ej. con `ruteo_invalido`)
    ((candidata, manifest),) = await _sql(
        tid, "select id, manifest from connection_releases where connection_id = :c and number = :n", c=cid, n=int(numero)
    )
    return tid, cid, reporte, candidata, manifest


async def _instancias(tid: uuid.UUID, cid: uuid.UUID) -> dict[str, uuid.UUID]:
    filas = await _sql(
        tid,
        "select a.key, i.id from agent_instances i join agents a on a.tenant_id = i.tenant_id and a.id = i.agent_id "
        "where i.connection_id = :c and i.deleted_at is null",
        c=cid,
    )
    return {k: i for k, i in filas}


REGLAS_SUSCRIPCIONES = {
    "preset_ventas_palabras",
    "preset_agenda_palabras",
    "preset_suscripciones_palabras",
    "preset_suscripciones_mi_plan",
}


async def test_preset_suscripciones_v2_con_ruteo_activo_de_punta_a_punta(_ruteo: None, monkeypatch: Any) -> None:
    """`agent-core db presets-v2 --aplicar` (su función) crea Ventas, Agenda y Suscripciones con las
    reglas `preset_*` del Coordinador; el build de la release no falla con `ruteo_invalido`; el
    General (por defecto) también declara los procedimientos de los especialistas y aun así una
    demo la atiende Agenda, por la regla o por el disparador."""
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    tid, cid, reporte, candidata, manifest = await _negocio_con_preset_suscripciones()
    assert {a.key for a in reporte.agentes} >= {"ventas", "agenda", "suscripciones"}
    assert set(reporte.reglas_nuevas) == REGLAS_SUSCRIPCIONES
    async with tenant_scope(tid):
        await promover(tid, cid, candidata, actor="test")
    componentes = {c["agent_key"]: c for c in manifest["components"]}
    declarados = {k: {p["id"] for p in c["procedures"]} for k, c in componentes.items() if c["role"] == "specialist"}
    assert CITA in declarados["agenda"] and CITA in declarados["general"]  # la ambigüedad existe
    assert componentes["general"]["is_default"] and not componentes["agenda"]["is_default"]
    await _modo(tid, "active")
    inst = await _instancias(tid, cid)

    # Por la regla del preset (palabra «agendar»).
    conv = await _conversacion(tid, cid)
    g = await _turno(tid, conv, monkeypatch, ["¿Para qué día?"], texto=DEMO)
    d = await _decision(tid, conv)
    assert d["route_source"] == "rule" and d["rule_id"] == "preset_agenda_palabras"
    assert d["active_instance_id"] == str(inst["agenda"]) and await _dueno(tid, conv) == inst["agenda"]
    assert g.enums[0] == [CITA]

    # Por el disparador, sin palabra de regla: el General también declara `agendar_cita`, pero va a Agenda.
    otra = await _conversacion(tid, cid)
    await _turno(tid, otra, monkeypatch, ["¿A qué hora?"], texto="¿tienen espacio el jueves?")
    d = await _decision(tid, otra)
    assert d["route_source"] == "trigger" and d["active_instance_id"] == str(inst["agenda"])
    assert d["candidates"][0] == {"procedure_id": CITA, "owner": "agenda", "score": d["candidates"][0]["score"]}

    # Un saludo sigue con el General (el de siempre).
    tercera = await _conversacion(tid, cid)
    await _turno(tid, tercera, monkeypatch, ["Hola."], texto="hola, buenas tardes")
    d = await _decision(tid, tercera)
    assert d["route_source"] == "default" and d["active_instance_id"] == str(inst["general"])


# ---------------------------------------------------------------- dogfooding de solvershub (HU-AOS-22)
# Frases propias de Ventas que solvershub publica desde el editor (runbook de solvershub-comercial,
# `runbooks/agent-os-especialistas-solvershub.md`): nombres de sus planes y preguntas de precio.
EJEMPLOS_VENTAS_SOLVERSHUB = [
    "cuánto cuesta el plan basic",
    "quiero contratar premium",
    "qué precio tiene el plan empresarial",
    "quiero empezar con el plan free",
    "me interesa el plan premium",
]


async def _ejemplos_de_ventas(tid: uuid.UUID, ejemplos: list[str]) -> None:
    """Versión nueva de Ventas con `overrides.examples` en `contratar_plan` (lo que hace el editor)."""
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    ((aid, contenido),) = await _sql(
        tid,
        "select a.id, v.content from agents a join agent_versions v on v.tenant_id = a.tenant_id and v.agent_id = a.id "
        "where a.key = 'ventas' order by v.version desc limit 1",
    )
    nuevo = dict(contenido)
    nuevo["procedures"] = [
        {**p, "overrides": {"examples": ejemplos}} if p["id"] == PLAN else p for p in nuevo["procedures"]
    ]
    async with tenant_scope(tid):
        await versiones.publicar(tid, aid, catalogo=catalogo_de_acciones(_modules()), contenido=nuevo)


async def _ruta(tid: uuid.UUID, cid: uuid.UUID, monkeypatch: Any, texto: str) -> dict[str, Any]:
    conv = await _conversacion(tid, cid)
    await _turno(tid, conv, monkeypatch, ["Claro."], texto=texto)
    return await _decision(tid, conv)


async def test_dogfooding_solvershub_ventas_agenda_y_suscripciones(_ruteo: None, monkeypatch: Any) -> None:
    """HU-AOS-22: el negocio con el preset `suscripciones` v2 y las frases propias de Ventas.
    En sombra atiende el General y se registra la ruta; en activo «quiero agendar una demo» va a
    Agenda, «quiero contratar Premium» a Ventas, «cambiar mi plan» a Suscripciones y un saludo
    al General. «cuánto cuesta el plan basic» se iba a Suscripciones (disparador de
    cambiar_plan) hasta publicar las frases de Ventas."""
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    tid, cid, reporte, candidata, _manifest = await _negocio_con_preset_suscripciones()
    assert set(reporte.reglas_nuevas) == REGLAS_SUSCRIPCIONES
    async with tenant_scope(tid):
        await promover(tid, cid, candidata, actor="test")
    inst = await _instancias(tid, cid)
    assert {"general", "ventas", "agenda", "suscripciones"} <= set(inst)

    # Sombra: decide y registra, pero atiende el General (el de siempre).
    await _modo(tid, "shadow")
    d = await _ruta(tid, cid, monkeypatch, DEMO)
    assert d["route_source"] == "default" and d["active_instance_id"] == str(inst["general"])
    assert d["shadow_route"]["agent_key"] == "agenda" and d["shadow_route"]["rule_id"] == "preset_agenda_palabras"

    # Activo, sin las frases de Ventas: la pregunta de precio cae en Suscripciones por el disparador.
    await _modo(tid, "active")
    d = await _ruta(tid, cid, monkeypatch, "cuánto cuesta el plan basic")
    assert d["route_source"] == "trigger" and d["active_instance_id"] == str(inst["suscripciones"])

    # Con las frases de Ventas publicadas y su release promovida.
    await _ejemplos_de_ventas(tid, EJEMPLOS_VENTAS_SOLVERSHUB)
    await _publicar_release(tid, cid)
    esperados = [
        (DEMO, "agenda", "rule", "preset_agenda_palabras"),
        ("quiero contratar Premium", "ventas", "trigger", None),
        ("cambiar mi plan", "suscripciones", "rule", "preset_suscripciones_mi_plan"),
        ("cuánto cuesta el plan basic", "ventas", "trigger", None),
        ("hola, buenas tardes", "general", "default", None),
    ]
    for texto, agente, fuente, regla in esperados:
        d = await _ruta(tid, cid, monkeypatch, texto)
        assert (d["route_source"], d["rule_id"]) == (fuente, regla), texto
        assert d["active_instance_id"] == str(inst[agente]), texto
