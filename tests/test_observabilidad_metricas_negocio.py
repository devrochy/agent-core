"""Métricas propias y sondas (B3, ADR-0188): etiquetas cerradas, ganchos en el loop, el turno,
Celery y Graph, sondas solo en la API y nunca un fallo hacia el camino que mide.

La prueba con el SDK real corre en un intérprete aparte (no deja fijado el `MeterProvider` global)
y se salta donde el SDK de métricas no carga (el Python del runner de CI no trae `_ctypes`)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import types
import uuid
import weakref
from types import SimpleNamespace
from typing import Any

import pytest

import core.observability.metricas_negocio as mn
import core.observability.sondas as sondas
from core.config import get_settings
from core.llm.types import LLMResponse, LLMUsageInfo


class _Registro:
    def __init__(self) -> None:
        self.puntos: list[tuple[float, dict[str, str]]] = []

    def add(self, valor: float, atributos: dict[str, str] | None = None) -> None:
        self.puntos.append((valor, dict(atributos or {})))

    record = add


@pytest.fixture
def registros(monkeypatch):
    r = {n: _Registro() for n in ("_tokens", "_costo", "_llamadas", "_turnos", "_tareas", "_duracion_tareas", "_errores_graph", "_http")}
    for nombre, reg in r.items():
        monkeypatch.setattr(mn, nombre, reg)
    return r


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [("deepseek-chat", "deepseek-chat"), ("WhatsApp", "whatsapp"), ("anthropic/claude-sonnet-4.5", "anthropic/claude-sonnet-4.5"),
     ("+57 300 123 4567", "otro"), ("ana@correo.com", "otro"), (None, "otro"), ("x" * 65, "otro"), ("RATE_LIMIT", "rate_limit")],
)
def test_etiquetas_cerradas(valor, esperado):
    assert mn._etiqueta(valor) == esperado


@pytest.mark.parametrize(
    ("ruta", "grupo"),
    [("/healthz", None), ("/healthz/deep", None), ("/webhooks/whatsapp", "webhooks"), ("/api/v1/agents/123", "api"),
     ("/api/pack/x", "api"), ("/admin/tenants", "admin"), ("/crm/clientes", "other"), ("/", "other")],
)
def test_grupo_de_ruta(ruta, grupo):
    assert mn.grupo_de_ruta(ruta) == grupo


def test_respuesta_y_fallo_del_llm(registros):
    mn.llm_respuesta("deepseek-chat", entrada=100, salida=20, cache_leido=70, cache_escrito=0, costo_usd=0.0012)
    mn.llm_fallo("deepseek-chat")
    assert sorted(registros["_tokens"].puntos, key=lambda p: p[1]["kind"]) == [
        (70, {"model": "deepseek-chat", "kind": "cache_read"}),
        (100, {"model": "deepseek-chat", "kind": "input"}),
        (20, {"model": "deepseek-chat", "kind": "output"}),
    ]
    assert registros["_costo"].puntos == [(0.0012, {"model": "deepseek-chat"})]
    assert [p[1]["outcome"] for p in registros["_llamadas"].puntos] == ["ok", "error"]


def test_el_uso_del_turno_registra_la_respuesta(monkeypatch):
    from core.orchestrator.loop import _TurnUsage

    vistos: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(mn, "llm_respuesta", lambda modelo, **kw: vistos.append((modelo, kw)))
    proveedor = types.SimpleNamespace(estimate_cost_usd=lambda i, o: 0.5, pricing_per_mtok=(0.0, 0.0))
    respuesta = LLMResponse(
        content="ok", stop_reason="end_turn", model="claude-x",
        usage=LLMUsageInfo(input_tokens=10, output_tokens=5, cache_read_tokens=80, cache_write_tokens=30, input_includes_cache=False),
    )
    _TurnUsage().add(proveedor, respuesta)
    ((modelo, kw),) = vistos
    assert modelo == "claude-x"
    assert kw["entrada"] == 120 and kw["salida"] == 5 and kw["cache_leido"] == 80 and kw["cache_escrito"] == 30


async def test_el_turno_registra_duracion_canal_y_resultado(monkeypatch):
    from core.kernel import decisions
    from core.shared.tenant import tenant_scope

    vistos: list[tuple[Any, Any, float]] = []
    monkeypatch.setattr(mn, "turno", lambda canal, outcome, s: vistos.append((canal, outcome, s)))

    @decisions.en_turno
    async def sin_decidir() -> None:
        return None

    async with tenant_scope(uuid.UUID("20000000-0000-0000-0000-0000000000aa")):
        await sin_decidir()
    ((canal, outcome, segundos),) = vistos
    assert (canal, outcome) == (None, "sin_decision") and segundos >= 0


def test_la_decision_guarda_el_canal(monkeypatch):
    from core.kernel import decisions

    tid = uuid.uuid5(uuid.NAMESPACE_URL, "metricas-canal")
    turno = decisions.Turno(id=uuid.uuid4(), tenant_id=tid, t0=0.0)
    monkeypatch.setattr(decisions, "turno_actual", lambda: turno)

    async def _sin_bd(*_a, **_k):
        raise RuntimeError("sin base en la prueba")

    monkeypatch.setattr(decisions, "get_session", _sin_bd)
    conversacion = SimpleNamespace(id=uuid.uuid4(), tenant_id=tid, channel="instagram")
    asyncio.run(decisions.decidir(conversacion, "routed"))
    assert turno.canal == "instagram"


def test_tareas_de_celery(registros):
    mn.tarea_empezo(task_id="t1")
    mn.tarea_termino(task_id="t1", task=SimpleNamespace(name="core.outbound_send"), state="SUCCESS")
    mn.tarea_termino(task_id="t2", task=SimpleNamespace(name="core.worker_heartbeat"), state="FAILURE")  # sin inicio
    assert [p[1] for p in registros["_tareas"].puntos] == [
        {"task": "core.outbound_send", "status": "success"},
        {"task": "core.worker_heartbeat", "status": "failure"},
    ]
    assert len(registros["_duracion_tareas"].puntos) == 1
    assert registros["_duracion_tareas"].puntos[0][1] == {}  # sin `task`: no multiplica los buckets


def test_las_senales_de_celery_estan_conectadas():
    from celery.signals import task_postrun, task_prerun

    import core.tasks.celery_app  # noqa: F401  (conecta las señales)

    def _receptores(senal):  # `weak=False` guarda la función; `weak=True`, una weakref
        return [r() if isinstance(r, weakref.ReferenceType) else r for _, r in senal.receivers]

    assert mn.tarea_empezo in _receptores(task_prerun)
    assert mn.tarea_termino in _receptores(task_postrun)


def test_error_de_graph(monkeypatch, registros):
    import httpx

    from core.gateway import meta_graph

    async def _nada(*_a, **_k):
        return None

    monkeypatch.setattr(meta_graph, "record_usage", _nada)
    monkeypatch.setattr(meta_graph, "_log_call", _nada)
    transporte = httpx.MockTransport(lambda req: httpx.Response(400, json={"error": {"code": 190, "message": "x"}}))
    original = httpx.AsyncClient

    monkeypatch.setattr(meta_graph.httpx, "AsyncClient", lambda **kw: original(transport=transporte, **kw))
    with pytest.raises(meta_graph.GraphError):
        asyncio.run(meta_graph.graph_fetch("/me", token="t", app_secret="s"))
    assert registros["_errores_graph"].puntos == [(1, {"category": "auth"})]


def test_una_metrica_rota_no_rompe(monkeypatch):
    class _Roto:
        def add(self, *_a, **_k):
            raise RuntimeError("exportador roto")

    monkeypatch.setattr(mn, "_llamadas", _Roto())
    mn.llm_fallo("m")  # no lanza


def test_middleware_http_por_grupo_y_clase(registros):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/webhooks/x")
    async def _w() -> dict:
        return {}

    @app.get("/api/v1/falla")
    async def _f() -> dict:
        raise RuntimeError("boom")

    @app.get("/healthz")
    async def _h() -> dict:
        return {}

    app.add_middleware(mn.MetricasHttpMiddleware)
    cliente = TestClient(app, raise_server_exceptions=False)
    cliente.get("/webhooks/x")
    cliente.get("/api/v1/falla")
    cliente.get("/healthz")
    cliente.get("/no-existe")
    assert [p[1] for p in registros["_http"].puntos] == [
        {"route_group": "webhooks", "status_class": "2xx"},
        {"route_group": "api", "status_class": "5xx"},
        {"route_group": "other", "status_class": "4xx"},
    ]


# --------------------------------------------------------------------------------- sondas
@pytest.fixture
def valores(monkeypatch):
    v: dict[str, Any] = {"dlq": None, "inbound": None, "latido": None, "deps": {}, "crons": {}}
    monkeypatch.setattr(sondas, "_valores", v)
    return v


def test_medir_llena_los_valores_y_los_gauges_solo_leen(monkeypatch, valores):
    async def _dlq():
        return 3

    async def _inbound():
        return 7

    async def _latido():
        return sondas.LATIDO_AUSENTE_S

    async def _crons():
        return {"core.billing_lifecycle": 2}

    async def _ok():
        return None

    async def _cae():
        raise ConnectionError("redis://:clave@host")

    monkeypatch.setattr(sondas, "_dlq", _dlq)
    monkeypatch.setattr(sondas, "_inbound_pendiente", _inbound)
    monkeypatch.setattr(sondas, "_latido", _latido)
    monkeypatch.setattr(sondas, "_crons_fallidos", _crons)
    monkeypatch.setattr(sondas.salud, "sonda_db", _ok)
    monkeypatch.setattr(sondas.salud, "sonda_redis", _cae)
    valores["crons"] = {"core.viejo": 1}

    asyncio.run(sondas.medir())

    assert [o.value for o in sondas._observar_dlq(None)] == [3]
    assert [o.value for o in sondas._observar_inbound(None)] == [7]
    assert [o.value for o in sondas._observar_latido(None)] == [9999.0]
    assert {o.attributes["component"]: o.value for o in sondas._observar_deps(None)} == {"db": 1, "redis": 0}
    # el cron que ya no aparece queda en 0, no desaparece
    assert {o.attributes["job"]: o.value for o in sondas._observar_crons(None)} == {"core.viejo": 0, "core.billing_lifecycle": 2}


def test_una_sonda_que_falla_queda_sin_dato(monkeypatch, valores):
    async def _cae():
        raise TimeoutError

    for nombre in ("_dlq", "_inbound_pendiente", "_latido", "_crons_fallidos"):
        monkeypatch.setattr(sondas, nombre, _cae)
    monkeypatch.setattr(sondas.salud, "sonda_db", _cae)
    monkeypatch.setattr(sondas.salud, "sonda_redis", _cae)
    asyncio.run(sondas.medir())
    assert list(sondas._observar_dlq(None)) == [] and list(sondas._observar_latido(None)) == []
    assert {o.attributes["component"]: o.value for o in sondas._observar_deps(None)} == {"db": 0, "redis": 0}


class _RedisInfo:
    async def xinfo_groups(self, _stream):
        return [{"name": "otro", "pending": 50, "lag": 50}, {"name": sondas.GRUPO_INBOUND, "pending": 2, "lag": None}]


def test_inbound_pendiente_del_grupo_del_orquestador(monkeypatch):
    monkeypatch.setattr(sondas.salud, "_redis", lambda: _RedisInfo())
    assert asyncio.run(sondas._inbound_pendiente()) == 2


@pytest.mark.parametrize(("rol", "encendidas", "arranca"), [("api", True, True), ("worker", True, False), ("api", False, False)])
def test_sondas_solo_en_la_api_con_metricas(monkeypatch, rol, encendidas, arranca):
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_core_role", rol)
    monkeypatch.setattr(settings, "otel_metrics_enabled", encendidas)

    async def _medir():
        return None

    monkeypatch.setattr(sondas, "medir", _medir)

    async def _probar():
        tarea = sondas.arrancar()
        if tarea is not None:
            tarea.cancel()
        return tarea is not None

    assert asyncio.run(_probar()) is arranca


# --------------------------------------------------------------------------- SDK real
def test_con_el_sdk_real_salen_los_nombres_y_etiquetas():
    pytest.importorskip("opentelemetry.sdk.metrics")
    codigo = textwrap.dedent(
        """
        import json
        from types import SimpleNamespace
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        import core.observability.metricas_negocio as mn
        import core.observability.sondas as sondas

        lector = InMemoryMetricReader()
        metrics.set_meter_provider(MeterProvider(metric_readers=[lector]))
        mn.llm_respuesta("deepseek-chat", entrada=10, salida=2, cache_leido=0, cache_escrito=0, costo_usd=0.01)
        mn.turno("whatsapp", "replied", 3.2)
        mn.tarea_empezo(task_id="a")
        mn.tarea_termino(task_id="a", task=SimpleNamespace(name="core.outbound_send"), state="SUCCESS")
        sondas._valores.update(dlq=0, inbound=1, latido=12.0, deps={"db": 1, "redis": 1}, crons={"core.x": 0})
        salida = {}
        for rm in lector.get_metrics_data().resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    puntos = m.data.data_points
                    salida[m.name] = [dict(p.attributes) for p in puntos]
                    if hasattr(puntos[0], "explicit_bounds"):
                        salida[m.name + ":bounds"] = list(puntos[0].explicit_bounds)
        print("R " + json.dumps(salida))
        """
    )
    r = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True, env=dict(os.environ), timeout=60)
    assert r.returncode == 0, r.stderr
    (linea,) = [x[2:] for x in r.stdout.splitlines() if x.startswith("R ")]
    datos = json.loads(linea)
    assert {"model": "deepseek-chat", "kind": "input"} in datos["agentcore_llm_tokens_total"]
    assert datos["agentcore_turn_duration_seconds"] == [{"channel": "whatsapp", "outcome": "replied"}]
    assert datos["agentcore_turn_duration_seconds:bounds"] == [0.5, 1, 2, 5, 10, 20, 30, 60, 120]
    assert datos["agentcore_celery_task_duration_seconds"] == [{}]
    assert datos["agentcore_dependency_up"] == [{"component": "db"}, {"component": "redis"}]
    for nombre in ("agentcore_dlq_length", "agentcore_inbound_pending", "agentcore_worker_heartbeat_age_seconds", "agentcore_cron_failures_24h"):
        assert nombre in datos
    prohibidas = {"tenant_id", "agent_id", "conversation_id", "phone", "telefono", "http.route", "path"}
    for nombre, puntos in datos.items():
        if not nombre.endswith(":bounds"):
            assert not any(prohibidas & set(p) for p in puntos), nombre


def test_el_grupo_es_el_del_consumer():
    from core.orchestrator.consumer import GROUP

    assert sondas.GRUPO_INBOUND == GROUP
