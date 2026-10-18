"""HU-AOS-15 · el loop ejecuta la release v3 con `aos_runtime = 'v3'` (adenda de ADR-0117).

Contra el stack real, con el loop real y un modelo guionado (sin LLM ni red):
- **Equivalencia legacy ↔ v3:** el mismo negocio (release 1 construida desde su AgentSpec v2 por
  `backfill_releases`) corre el mismo turno en `legacy` y en `v3`: las mismas tools ofrecidas, el
  mismo prompt del sistema (sin la fecha y hora) y la misma respuesta. Con v3 quedan escritos
  `conversations.active_agent_instance_id`, `usage_ledger.{release_id, agent_instance_id,
  agent_version_id, connection_id}`, `core_decisions.active_instance_id` y
  `turn_outcomes.final_instance_id`; con legacy, NULL.
- **Rollback completo con dos releases:** la release 2 lleva otra versión del General (otro
  modelo, otra frase prohibida, un binding deshabilitado, sin `perfil` en `writeNamespaces`): el
  siguiente turno la ejecuta (modelo, denegación, frase prohibida y memoria cambian); tras el
  rollback vuelve todo.
- **Dos negocios a la vez** (uno legacy, uno v3): nada se cruza.
- **`migrar-a-v3`:** precondiciones, dry-run, encendido, release desactualizada, `--construir` y
  `--volver`.
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

CATALOGO = {
    "pack": "pack_commerce",
    "category_paths": [["Planes"]],
    "products": [
        {"name": "Plan Premium", "description": "Todo incluido.", "category_path": ["Planes"],
         "variants": [{"sku": "PREMIUM-MES", "price": 600000, "stock": 0,
                       "attributes": {"periodicidad": "mensual", "trial_dias": 0}}]},
    ],
}
MENSAJE = "Hola, ¿qué planes tienen?"
GUION: list[Any] = [("consultar_inventario", {}), "Tenemos el plan Premium mensual."]


# ---------------------------------------------------------------- helpers
def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _banderas(tid: uuid.UUID, runtime: str, procedimientos: bool = False) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("update tenants set aos_runtime = :r, aos_procedures = :p where id = :t"),
                {"t": tid, "r": runtime, "p": procedimientos},
            )
    finally:
        await engine.dispose()


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _negocio(slug: str, pagos: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """Negocio con commerce + payments y una conexión conversacional. (tid, conexión)."""
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
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid, t=tid, ext=f"ig-{cid.hex[:8]}",
    )
    journeys._CONEXION_V3[tid] = cid  # las conversaciones de la corrida nacen en la conexión
    return tid, cid


async def _release_uno(tid: uuid.UUID) -> uuid.UUID:
    from core.releases.release_uno import backfill_releases

    reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte.errores
    [(rid,)] = await _sql(tid, "select active_release_id from channel_connections where tenant_id = :t "
                               "and type_id = 'instagram_meta'", t=tid)
    assert rid is not None
    return rid


@pytest.fixture(scope="module")
async def negocios(request) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID, uuid.UUID, Any]]:
    """A (migra a v3 durante los tests) y B (legacy siempre), cada uno con su release 1."""
    import pack_payments.payments as capacidad

    from core.config import get_settings
    from core.releases.packs import sync_packs
    from core.tenants import deactivate_tenant, purge_tenant

    original = capacidad.build_provider
    await journeys._preparar_proceso()
    assert not (await sync_packs(_modules())).fallo
    pagos = journeys.PagoSimulado(monto_minimo=150000)
    slugs = [f"loopv3-a-{uuid.uuid4().hex[:6]}", f"loopv3-b-{uuid.uuid4().hex[:6]}"]
    a, cid_a = await _negocio(slugs[0], pagos)
    b, _ = await _negocio(slugs[1], pagos)
    await _release_uno(a)
    await _release_uno(b)
    # v3 exige AOS_CONNECTION_ROUTING (ADR-0112): encendida para todo el módulo.
    settings = get_settings()
    antes = settings.aos_connection_routing
    settings.aos_connection_routing = True
    yield a, cid_a, b, pagos
    settings.aos_connection_routing = antes
    capacidad.build_provider = original
    for tid, slug in zip((a, b), slugs, strict=True):
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


class _Guion:
    """Modelo guionado: un paso por llamada. Guarda las tools ofrecidas, el prompt del sistema y
    el modelo que el loop resolvió."""

    provider_name = "fake"
    model = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Any]) -> None:
        self.pasos = list(pasos)
        self.tools: list[list[str]] = []
        self.system: list[str] = []
        self.resultados: list[str] = []
        self.modelos: list[str] = []

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

    def _proveedor(*_a: Any, **kwargs: Any) -> _Guion:
        guion.modelos.append(str(kwargs.get("model")))
        return guion

    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", _proveedor)
    resultado = await journeys._turno(ctx, mensaje)
    return resultado, guion


async def _cliente(tid: uuid.UUID, pagos: Any, nombre: str = "Rob") -> Any:
    customer_id, conversation_id = await journeys.nuevo_cliente(tid, nombre)
    return journeys.Contexto(tid, customer_id, conversation_id, pagos)


def _sin_fecha(prompt: str) -> str:
    from core.llm.prompt_builder import CURRENT_DATETIME_HEADER

    i = prompt.find(CURRENT_DATETIME_HEADER)
    return prompt if i < 0 else prompt[:i]


async def _trazas(tid: uuid.UUID, ctx: Any) -> dict[str, Any]:
    [(dueno,)] = await _sql(tid, "select active_agent_instance_id from conversations where id = :c", c=ctx.conversation_id)
    [uso] = await _sql(
        tid,
        "select release_id, agent_instance_id, agent_version_id, connection_id from usage_ledger "
        "where tenant_id = :t and conversation_id = :c order by created_at desc limit 1",
        t=tid, c=ctx.conversation_id,
    )
    [decision] = await _sql(
        tid,
        "select d.active_instance_id, d.focus_task_id, d.release_id, o.final_instance_id from core_decisions d "
        "left join turn_outcomes o on o.turn_id = d.id where d.tenant_id = :t and d.conversation_id = :c "
        "order by d.created_at desc limit 1",
        t=tid, c=ctx.conversation_id,
    )
    return {"dueno": dueno, "uso": tuple(uso), "decision": tuple(decision)}


# ---------------------------------------------------------------- equivalencia
async def test_el_mismo_turno_en_legacy_y_en_v3_ofrece_lo_mismo_y_deja_las_trazas(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a, cid, _b, pagos = negocios
    await _banderas(a, "legacy")
    ctx_legacy = await _cliente(a, pagos)
    r_legacy, g_legacy = await _turno(ctx_legacy, monkeypatch, MENSAJE, list(GUION))
    trazas_legacy = await _trazas(a, ctx_legacy)
    assert trazas_legacy["dueno"] is None and trazas_legacy["uso"] == (None, None, None, None)
    assert trazas_legacy["decision"][0] is None and trazas_legacy["decision"][3] is None

    await _banderas(a, "v3")
    ctx_v3 = await _cliente(a, pagos)
    r_v3, g_v3 = await _turno(ctx_v3, monkeypatch, MENSAJE, list(GUION))
    assert not r_legacy.escalated and not r_v3.escalated
    assert r_v3.text == r_legacy.text
    assert g_v3.tools == g_legacy.tools and g_v3.tools[0]  # las mismas tools, en cada vuelta
    assert g_v3.modelos == g_legacy.modelos
    assert _sin_fecha(g_v3.system[0]) == _sin_fecha(g_legacy.system[0])
    assert g_v3.resultados == g_legacy.resultados

    [(rid,)] = await _sql(a, "select active_release_id from channel_connections where id = :c", c=cid)
    [(general,)] = await _sql(
        a,
        "select ai.id from agent_instances ai join agents ag on ag.tenant_id = ai.tenant_id and ag.id = ai.agent_id "
        "where ai.tenant_id = :t and ai.connection_id = :c and ag.key = 'general' and ai.deleted_at is null",
        t=a, c=cid,
    )
    [(version,)] = await _sql(a, "select agent_version_id from release_components where release_id = :r "
                                 "and agent_instance_id = :i", r=rid, i=general)
    trazas = await _trazas(a, ctx_v3)
    assert trazas["dueno"] == general
    assert trazas["uso"] == (rid, general, version, cid)
    assert trazas["decision"] == (general, None, rid, general)
    [(version_ledger,)] = await _sql(a, "select agent_spec_version from usage_ledger where tenant_id = :t and "
                                        "conversation_id = :c", t=a, c=ctx_v3.conversation_id)
    assert version_ledger.startswith("general@v3:")


# ---------------------------------------------------------------- rollback completo
async def _publicar_general(tid: uuid.UUID, cambios: dict[str, Any]) -> uuid.UUID:
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    [(agent_id, contenido)] = await _sql(
        tid,
        "select v.agent_id, v.content from agent_versions v join agents a on a.tenant_id = v.tenant_id and a.id = v.agent_id "
        "where v.tenant_id = :t and a.key = 'general' order by v.version desc limit 1",
        t=tid,
    )
    nuevo = {**contenido, **cambios}
    async with tenant_scope(tid):
        version, creada = await versiones.publicar(
            tid, agent_id, catalogo=catalogo_de_acciones(_modules()), contenido=nuevo, label="dos", notes="release 2"
        )
    assert creada
    return version.id


async def _construir_y_promover(tid: uuid.UUID, cid: uuid.UUID) -> uuid.UUID:
    from core.releases.build import construir
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        resultado = await construir(tid, cid, actor="test", modules=_modules())
        await promover(tid, cid, resultado.release_id, actor="test")
    return resultado.release_id


async def test_rollback_completo_cambia_modelo_tools_guardrails_y_memoria(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a, cid, _b, pagos = negocios
    await _banderas(a, "v3")
    [(uno,)] = await _sql(a, "select active_release_id from channel_connections where id = :c", c=cid)
    [(contenido,)] = await _sql(a, "select content from agent_versions where tenant_id = :t and id = ("
                                   "select agent_version_id from release_components where release_id = :r and agent_instance_id = ("
                                   "select ai.id from agent_instances ai join agents ag on ag.id = ai.agent_id "
                                   "where ai.connection_id = :c and ag.key = 'general'))", t=a, r=uno, c=cid)
    bindings = [dict(b) for b in contenido["tools"]]
    for b in bindings:
        if b["tool"] == "consultar_inventario":
            b["enabled"] = False
    version_dos = await _publicar_general(a, {
        "model": {**contenido["model"], "primary": "modelo-dos"},
        "persona": {**contenido["persona"], "forbiddenPhrases": ["plan premium"]},
        "memory": {**contenido["memory"], "writeNamespaces": []},
        "tools": bindings,
    })
    dos = await _construir_y_promover(a, cid)
    assert dos != uno

    guion_dos: list[Any] = [
        ("consultar_inventario", {}),
        ("update_customer_profile", {"attributes": {"ciudad": "Cali"}}),
        "Tenemos el plan premium.",  # frase prohibida en la release 2: reintento y escalamiento
        "Tenemos el plan premium mensual.",
    ]
    ctx = await _cliente(a, pagos)
    resultado, guion = await _turno(ctx, monkeypatch, MENSAJE, list(guion_dos))
    assert guion.modelos[0] == "modelo-dos"
    assert "deshabilitada" in guion.resultados[0]
    assert "no puede guardar datos" in guion.resultados[1]
    assert resultado.escalated
    trazas = await _trazas(a, ctx)
    assert trazas["uso"][0] == dos and trazas["uso"][2] == version_dos

    # Rollback a la 1: el siguiente turno (misma conversación, a mitad de episodio) vuelve a todo.
    from core.releases.promocion import rollback
    from core.shared.tenant import tenant_scope

    async with tenant_scope(a):
        await rollback(a, cid, actor="test")
    await _sql(a, "update conversations set status = 'open', bot_paused = false where id = :c", c=ctx.conversation_id)
    resultado, guion = await _turno(ctx, monkeypatch, "¿Y el precio?", list(guion_dos))
    assert guion.modelos[0] != "modelo-dos"
    assert "deshabilitada" not in guion.resultados[0]
    assert "no puede guardar datos" not in guion.resultados[1] and '"status": "ok"' in guion.resultados[1]
    assert not resultado.escalated and resultado.text == "Tenemos el plan premium."
    trazas = await _trazas(a, ctx)
    assert trazas["uso"][0] == uno and trazas["uso"][2] != version_dos
    [(ciudad,)] = await _sql(a, "select attributes->>'ciudad' from customers where id = :c", c=ctx.customer_id)
    assert ciudad == "Cali"


# ---------------------------------------------------------------- dos negocios
async def test_un_negocio_legacy_y_otro_v3_a_la_vez_no_se_cruzan(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    a, _cid, b, pagos = negocios
    await _banderas(a, "v3")
    await _banderas(b, "legacy")
    ctx_a = await _cliente(a, pagos)
    ctx_b = await _cliente(b, pagos, nombre="Ana")
    _, g_a = await _turno(ctx_a, monkeypatch, MENSAJE, list(GUION))
    _, g_b = await _turno(ctx_b, monkeypatch, MENSAJE, list(GUION))
    assert g_a.tools == g_b.tools
    trazas_a, trazas_b = await _trazas(a, ctx_a), await _trazas(b, ctx_b)
    assert trazas_b["dueno"] is None and trazas_b["uso"] == (None, None, None, None) and trazas_b["decision"][0] is None
    assert trazas_a["dueno"] is not None
    # Desde B no se ve nada de A (RLS y filtro explícito), ni al revés.
    assert await _sql(b, "select id from agent_instances where id = :i", i=trazas_a["dueno"]) == []
    assert await _sql(b, "select id from usage_ledger where conversation_id = :c", c=ctx_a.conversation_id) == []
    assert await _sql(a, "select id from core_decisions where conversation_id = :c", c=ctx_b.conversation_id) == []


# ---------------------------------------------------------------- dueño y episodio
async def test_el_dueno_sobrevive_en_el_episodio_y_se_suelta_al_empezar_otro(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Bloqueante 1 del arquitecto: el dueño vigente atiende mientras dure el episodio; un episodio
    nuevo lo suelta y atiende el `routing.defaultAgent` (aquí, el General). Nunca se escribe un
    handoff: solo `active_agent_instance_id` (+ `owner_since`)."""
    from core.agents import versiones
    from core.agents.instancias import agregar_instancia
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    a, cid, _b, pagos = negocios
    await _banderas(a, "v3")
    [(contenido,)] = await _sql(
        a,
        "select v.content from agent_versions v join agents ag on ag.tenant_id = v.tenant_id and ag.id = v.agent_id "
        "where v.tenant_id = :t and ag.key = 'general' order by v.version desc limit 1",
        t=a,
    )
    async with tenant_scope(a):
        ventas = await versiones.crear_agente(a, key=f"ventas_{uuid.uuid4().hex[:4]}", kind="specialist", name="Ventas")
        await versiones.publicar(a, ventas.id, catalogo=catalogo_de_acciones(_modules()), contenido=contenido, label="uno")
        instancia_ventas = await agregar_instancia(a, cid, ventas.id)
    await _construir_y_promover(a, cid)

    ctx = await _cliente(a, pagos)
    await _turno(ctx, monkeypatch, MENSAJE, list(GUION))
    general = (await _trazas(a, ctx))["dueno"]
    assert general is not None and general != instancia_ventas

    # Dueño = Ventas (lo que haría un handoff de HU-AOS-19): a mitad de episodio sigue atendiendo.
    await _sql(a, "update conversations set active_agent_instance_id = :i where id = :c", i=instancia_ventas,
               c=ctx.conversation_id)
    await _turno(ctx, monkeypatch, "¿Y el precio?", ["Cuesta 600.000."])
    trazas = await _trazas(a, ctx)
    assert trazas["dueno"] == instancia_ventas and trazas["uso"][1] == instancia_ventas
    assert trazas["decision"][0] == instancia_ventas

    # Episodio nuevo (la conversación quedó inactiva más que `idleMinutes`): se suelta al dueño.
    await _sql(a, "update messages set created_at = created_at - interval '3 days' where conversation_id = :c",
               c=ctx.conversation_id)
    await _turno(ctx, monkeypatch, "Hola de nuevo", ["Hola otra vez."])
    trazas = await _trazas(a, ctx)
    assert trazas["dueno"] == general and trazas["uso"][1] == general and trazas["decision"][0] == general
    # Nunca se escribe un handoff en la parte 1.
    assert await _sql(a, "select 1 from information_schema.tables where table_name = 'conversation_handoffs'") == [] or (
        await _sql(a, "select id from conversation_handoffs where conversation_id = :c", c=ctx.conversation_id) == []
    )


# ---------------------------------------------------------------- migrar-a-v3
async def test_migrar_a_v3_precondiciones_encendido_y_vuelta(negocios) -> None:  # type: ignore[no-untyped-def]
    from core.agents.spec import load_active_spec, save_spec_version
    from core.config import get_settings
    from core.releases.migracion_v3 import migrar_a_v3
    from core.shared.tenant import tenant_scope

    _a, _cid, b, pagos = negocios
    del pagos
    await _banderas(b, "legacy")
    url = get_settings().migrations_database_url

    async def correr(**kw: Any) -> Any:
        reporte = await migrar_a_v3(tenant_ids=[b], modules=_modules(), database_url=url, **kw)
        [negocio] = reporte.negocios
        return negocio

    # Sin la bandera global: bloqueado.
    settings = get_settings()
    settings.aos_connection_routing = False
    try:
        n = await correr(dry_run=True)
    finally:
        settings.aos_connection_routing = True
    assert n.accion == "bloqueado" and "AOS_CONNECTION_ROUTING" in n.bloqueos[0]

    # Listo en dry-run (release 1 al día); encendido de verdad; idempotente.
    n = await correr(dry_run=True)
    assert n.accion == "listo" and not n.bloqueos, n.bloqueos
    n = await correr(dry_run=False)
    assert n.accion == "encendido"
    [(runtime, procs)] = await _sql(None, "select aos_runtime, aos_procedures from tenants where id = :t", t=b)
    assert (runtime, procs) == ("v3", False)
    assert (await correr(dry_run=True)).accion == "ya_estaba"

    # Vuelta: con el motor encendido se rechaza salvo --apagar-procedimientos.
    await _banderas(b, "v3", procedimientos=True)
    n = await correr(dry_run=False, volver=True)
    assert n.accion == "bloqueado" and "aos_procedures" in n.bloqueos[0]
    n = await correr(dry_run=False, volver=True, apagar_procedimientos=True)
    assert n.accion == "apagado"
    [(runtime, procs)] = await _sql(None, "select aos_runtime, aos_procedures from tenants where id = :t", t=b)
    assert (runtime, procs) == ("legacy", False)

    # El v2 editado después de la release 1: desactualizada; con --construir se reconstruye y enciende.
    async with tenant_scope(b):
        spec = await load_active_spec(b)
        await save_spec_version(spec.model_copy(update={"persona": spec.persona.model_copy(update={"maxLength": 321})}), b)
    n = await correr(dry_run=True)
    assert n.accion == "bloqueado" and any("desactualizada" in x for x in n.bloqueos), n.bloqueos
    n = await correr(dry_run=False, construir=True)
    assert n.accion == "encendido" and any("construida" in x for x in n.avisos), (n.bloqueos, n.avisos)
    [(numero,)] = await _sql(b, "select max(number) from connection_releases where tenant_id = :t", t=b)
    assert numero >= 2
    await _banderas(b, "legacy")

    # Bloqueante 2: un General sin `perfil` en writeNamespaces o con una declarativa con secretos en
    # la URL bloquea el negocio aunque la release esté al día.
    from pydantic import SecretStr

    from core.agents.secretos import SecretoDeclarativo, guardar_secretos_declarativos

    # En v3 el build no refresca desde el v2: congela la versión publicada (y el chequeo no compara con el v2).
    await _banderas(b, "v3")
    [(cid_b,)] = await _sql(b, "select id from channel_connections where tenant_id = :t and type_id = 'instagram_meta'", t=b)
    async with tenant_scope(b):
        await guardar_secretos_declarativos(
            b, {"transportadora_token": SecretoDeclarativo(SecretStr("tk-de-prueba"), ("api.transportadora.co",))}
        )
    await _publicar_general(b, {
        "memory": {**(await _contenido_general(b))["memory"], "writeNamespaces": []},
        "declarativeTools": [{
            "name": "consultar_envio", "method": "GET",
            "url": "https://api.transportadora.co/v1/guias?token={secret:transportadora_token}",
            "allowedDomains": ["api.transportadora.co"], "headers": {},
        }],
    })
    await _construir_y_promover(b, cid_b)
    n = await correr(dry_run=True)
    assert n.accion == "bloqueado", n
    assert any("writeNamespaces" in x for x in n.bloqueos), n.bloqueos
    assert any("consultar_envio" in x for x in n.bloqueos), n.bloqueos
    await _banderas(b, "legacy")


async def _contenido_general(tid: uuid.UUID) -> dict[str, Any]:
    [(contenido,)] = await _sql(
        tid,
        "select v.content from agent_versions v join agents a on a.tenant_id = v.tenant_id and a.id = v.agent_id "
        "where v.tenant_id = :t and a.key = 'general' order by v.version desc limit 1",
        t=tid,
    )
    return dict(contenido)
