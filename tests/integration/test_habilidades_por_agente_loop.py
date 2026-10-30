"""HU-AOS-16 (parte 1) · procedimientos por agente con el loop real y un modelo guionado (sin LLM).

Negocio `aos_runtime = v3` con el motor de procedimientos (`aos_procedures`), `pack_commerce` y
`pack_calendar`, y `AOS_CONNECTION_ROUTING` encendida (la ranura de la release solo se llena así):

- **El General ve lo mismo que sin release** (B3 del arquitecto): con la release del General por
  defecto, las tools ofrecidas (incluidas las puertas) y el enum de `iniciar_procedimiento` son
  los mismos que en una conversación sin release; los procedimientos abiertos `0.0.0` no entran.
- **Cada especialista ve lo suyo:** con Ventas (`contratar_plan ^2.0`) por defecto el enum es solo
  `contratar_plan` y no hay tools de la agenda; con Agenda (`agendar_cita ^1.0`), al revés. Un
  `iniciar_procedimiento` ajeno se rechaza y una tool de otro pack no corre.
- **Cambio de release sin escalar:** una tarea abierta de `contratar_plan 1.2.0` sigue
  (`revalidated`) cuando la conexión cambia a otra release con el mismo procedimiento.
- **Aislamiento:** la release de otro negocio no cambia lo que ve este.
"""

from __future__ import annotations

import os
import uuid
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

PLAN = "pack_subscriptions.contratar_plan"
CITA = "pack_calendar.agendar_cita"
PACKS = ["pack_commerce", "pack_subscriptions", "pack_calendar"]
DE_AGENDA = {"list_appointment_types", "check_availability", "find_next_available", "create_calendar_event"}
DE_VENTAS = {"consultar_inventario", "confirmar_cotizacion", "crear_pedido"}
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

    slug = f"aos16-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"AOS16 {slug}", slug=slug, packs=PACKS, sembrar_ejemplos=False, plan="premium")
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
        contenido["procedures"] = [{"id": proc, "version": "^2.0" if proc == PLAN else "^1.0"}]
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


# ---------------------------------------------------------------- el General: lo mismo que sin release
async def test_el_general_con_release_ve_lo_mismo_que_sin_release(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid = await _negocio()
    rid = await _publicar_release(tid, cid)
    ((manifest,),) = await _sql(tid, "select manifest from connection_releases where id = :r", r=rid)
    assert manifest["schema"] == "release/2"
    (general,) = [c for c in manifest["components"] if c["agent_key"] == "general"]
    # El General DECLARA los publicados de los packs habilitados (rango ^M.0, resuelto en el build).
    assert {p["id"] for p in general["procedures"] if p["version"] != "0.0.0"} == {
        PLAN, CITA, "pack_commerce.tomar_pedido", "pack_subscriptions.cambiar_plan",
    }

    sin_release = await _conversacion(tid, None)
    con_release = await _conversacion(tid, cid)
    a = await _turno(tid, sin_release, monkeypatch, ["Hola, ¿en qué te ayudo?"])
    b = await _turno(tid, con_release, monkeypatch, ["Hola, ¿en qué te ayudo?"])
    assert await _release_del_turno(tid, sin_release) is None
    assert await _release_del_turno(tid, con_release) == rid  # el turno sí tuvo release
    assert b.tools == a.tools
    assert b.enums == a.enums
    assert a.enums[0] and PLAN in a.enums[0] and CITA in a.enums[0]
    assert not any(pid.startswith(("core.", "tenant.")) for pid in b.enums[0] or [])  # sin abiertos


# ---------------------------------------------------------------- especialistas: cada uno lo suyo
async def test_cada_especialista_ve_solo_lo_suyo(_ruteo: None, monkeypatch: Any) -> None:
    tid, cid = await _negocio()
    ventas = await _especialista(tid, cid, "ventas", PLAN, {"consultar_inventario"}, defecto=True)
    agenda = await _especialista(tid, cid, "agenda", CITA, {"list_appointment_types", "check_availability"}, defecto=False)
    rid = await _publicar_release(tid, cid)

    conv = await _conversacion(tid, cid)
    g = await _turno(tid, conv, monkeypatch, [
        ("iniciar_procedimiento", {"procedure_id": CITA}),  # ajeno: se rechaza
        ("check_availability", {"date": "2030-01-07"}),  # tool de otro pack: no corre
        "¿Qué plan te interesa?",
    ])
    assert await _release_del_turno(tid, conv) == rid
    assert g.enums[0] == [PLAN]
    assert not (set(g.tools[0]) & DE_AGENDA)
    assert {"escalate_to_human", "iniciar_procedimiento"} <= set(g.tools[0])  # core y comandos siguen
    assert "consultar_inventario" in g.tools[0]
    assert len(g.resultados) == 2 and all("error" in r.lower() or "no " in r.lower() for r in g.resultados)
    assert await _tareas(tid, conv) == []

    await _por_defecto(tid, cid, agenda)
    rid2 = await _publicar_release(tid, cid)
    assert rid2 != rid
    otra = await _conversacion(tid, cid)
    g = await _turno(tid, otra, monkeypatch, [("iniciar_procedimiento", {"procedure_id": CITA}), "¿Para qué día?"])
    assert await _release_del_turno(tid, otra) == rid2
    assert g.enums[0] == [CITA]
    assert not (set(g.tools[0]) & DE_VENTAS)
    assert [(p, s) for p, s in await _tareas(tid, otra)] == [(CITA, "active")]
    del ventas


# ---------------------------------------------------------------- cambio de release sin escalar
async def test_una_tarea_publicada_sigue_al_cambiar_de_release(_ruteo: None) -> None:
    from core.db.session import get_session
    from core.kernel import state
    from core.kernel.release_del_turno import asignar
    from core.releases.cargar import cargar_release
    from core.shared.tenant import tenant_scope

    tid, cid = await _negocio()
    await _especialista(tid, cid, "ventas", PLAN, {"consultar_inventario"}, defecto=True)
    uno = await _publicar_release(tid, cid)
    conv = await _conversacion(tid, cid)
    t0 = datetime.now(UTC) - timedelta(days=2)
    await _mensaje(tid, conv, t0)
    async with tenant_scope(tid):
        async with get_session() as session:
            asignada = await asignar(session, tenant_id=tid, conversation_id=conv, connection_id=cid)
            assert asignada is not None and asignada[0].release_id == uno
            release = await cargar_release(session, tid, uno)
            assert release is not None
            proc = release.procedimientos()[PLAN]
            tarea = await state.iniciar(
                session, tenant_id=tid, conversation_id=conv, procedure_id=PLAN,
                procedure_version=proc.version, stage=proc.stages[0].id, actor="kernel", now=t0,
            )

    await _sql(tid, "update app_settings set assistant_name = 'Otra' where tenant_id = :t", t=tid)
    dos = await _publicar_release(tid, cid)
    assert dos != uno
    await _sql(tid, "update conversation_tasks set updated_at = now() where conversation_id = :c", c=conv)
    await _mensaje(tid, conv, t0 + timedelta(minutes=1), "outbound")
    await _mensaje(tid, conv)
    async with tenant_scope(tid):
        async with get_session() as session:
            asignada = await asignar(session, tenant_id=tid, conversation_id=conv, connection_id=cid)
    assert asignada is not None
    a = asignada[0]
    assert a.release_id == dos
    assert a.revalidadas == (tarea.id,) and not a.canceladas and not a.escaladas
    assert await _tareas(tid, conv) == [(PLAN, "active")]


# ---------------------------------------------------------------- aislamiento entre negocios
async def test_la_release_de_otro_negocio_no_cambia_lo_que_ve_este(_ruteo: None, monkeypatch: Any) -> None:
    a, cid_a = await _negocio()
    b, cid_b = await _negocio()
    await _especialista(a, cid_a, "ventas", PLAN, {"consultar_inventario"}, defecto=True)
    rid_a = await _publicar_release(a, cid_a)
    rid_b = await _publicar_release(b, cid_b)

    conv_b = await _conversacion(b, cid_b)
    g = await _turno(b, conv_b, monkeypatch, ["Hola."])
    assert await _release_del_turno(b, conv_b) == rid_b
    assert CITA in (g.enums[0] or [])  # B sigue con su General: ve todo lo suyo
    # La release de A no existe para B (RLS + tenant_id).
    assert await _sql(b, "select id from connection_releases where id = :r", r=rid_a) == []
    conv_a = await _conversacion(a, cid_a)
    g = await _turno(a, conv_a, monkeypatch, ["Hola."])
    assert g.enums[0] == [PLAN]
