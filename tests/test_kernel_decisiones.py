"""Kernel del Coordinador sin base de datos (HU-AOS-10): turno reentrante, status del
resultado, `trace_id` de respaldo, guarda de negocio del `turn_id` y traza en Celery."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from core.kernel import decisions
from core.shared import turno as turno_vigente
from core.shared.tenant import tenant_scope

T1 = uuid.UUID("20000000-0000-0000-0000-000000000001")
T2 = uuid.UUID("20000000-0000-0000-0000-000000000002")


async def test_el_turno_es_reentrante_y_se_cierra_una_vez() -> None:
    vistos: list[decisions.Turno | None] = []

    @decisions.en_turno(anota_resultado=True)
    async def interno() -> SimpleNamespace:
        vistos.append(decisions.turno_actual())
        return SimpleNamespace(text="hola", escalated=False)

    @decisions.en_turno
    async def externo() -> None:
        vistos.append(decisions.turno_actual())
        await interno()
        vistos.append(decisions.turno_actual())

    async with tenant_scope(T1):
        await externo()
    assert vistos[0] is not None and vistos[0] is vistos[1] is vistos[2]
    assert vistos[0].status == "replied"
    assert decisions.turno_actual() is None, "no se filtra fuera del turno"


async def test_sin_negocio_no_hay_turno() -> None:
    @decisions.en_turno
    async def f() -> decisions.Turno | None:
        return decisions.turno_actual()

    assert await f() is None


@pytest.mark.parametrize(
    ("resultado", "neutral", "status"),
    [
        (None, False, "silent"),
        (SimpleNamespace(text="", escalated=False), False, "silent"),
        (SimpleNamespace(text="ok", escalated=False), False, "replied"),
        (SimpleNamespace(text="ok", escalated=True), False, "escalated"),
        (SimpleNamespace(text="verifico", escalated=False), True, "neutral"),
    ],
)
async def test_status_desde_el_resultado_del_loop(resultado, neutral, status) -> None:
    @decisions.en_turno
    async def f() -> str | None:
        if neutral:
            decisions.marcar_neutral()
        decisions.anotar_resultado(resultado)
        turno = decisions.turno_actual()
        return turno.status if turno else None

    async with tenant_scope(T1):
        assert await f() == status


async def test_con_otel_apagado_el_trace_id_es_el_turn_id_y_viaja_en_traceparent() -> None:
    @decisions.en_turno
    async def f() -> tuple[uuid.UUID, str | None, dict[str, str]]:
        turno = decisions.turno_actual()
        assert turno is not None
        headers: dict[str, str] = {}
        propagate.inject(headers)
        return turno.id, turno.trace_id, headers

    async with tenant_scope(T1):
        turn_id, trace_id, headers = await f()
    assert trace_id == turn_id.hex
    assert headers["traceparent"].split("-")[1] == turn_id.hex


async def test_una_excepcion_deja_el_turno_en_failed_y_se_relanza() -> None:
    capturado: list[decisions.Turno] = []

    @decisions.en_turno
    async def f() -> None:
        turno = decisions.turno_actual()
        assert turno is not None
        capturado.append(turno)
        raise RuntimeError("boom")

    async with tenant_scope(T1):
        with pytest.raises(RuntimeError):
            await f()
    assert capturado[0].status == "failed"


async def test_el_turn_id_solo_vale_para_filas_del_mismo_negocio() -> None:
    turn_id = uuid.uuid4()
    token = turno_vigente.fijar(
        turno_vigente.TurnoVigente(turn_id=turn_id, tenant_id=T1, trace_id="t")
    )
    try:
        fila = SimpleNamespace(get_current_parameters=lambda: {"tenant_id": T1})
        ajena = SimpleNamespace(get_current_parameters=lambda: {"tenant_id": T2})
        assert turno_vigente.turn_id_de_fila(fila) == turn_id
        assert turno_vigente.trace_id_de_fila(fila) == "t"
        assert turno_vigente.turn_id_de_fila(ajena) is None
        async with tenant_scope(T2):
            assert turno_vigente.turno_id() is None
        async with tenant_scope(T1):
            assert turno_vigente.turno_id() == turn_id
    finally:
        turno_vigente.soltar(token)
    assert (
        turno_vigente.turn_id_de_fila(
            SimpleNamespace(get_current_parameters=lambda: {"tenant_id": T1})
        )
        is None
    )


def test_la_tarea_celery_comparte_el_trace_id_del_turno(monkeypatch) -> None:
    from core.observability import celery_trazas

    exportador = InMemorySpanExporter()
    proveedor = TracerProvider()
    proveedor.add_span_processor(SimpleSpanProcessor(exportador))
    tracer = proveedor.get_tracer("test")
    monkeypatch.setattr(celery_trazas, "get_tracer", lambda: tracer)

    headers: dict[str, str] = {}
    with tracer.start_as_current_span("kernel.turn") as turno:
        celery_trazas.al_publicar(headers=headers)
    tarea = SimpleNamespace(name="core.outbound_send", request=SimpleNamespace(**headers))
    celery_trazas.al_empezar(task_id="t-1", task=tarea)
    assert trace.get_current_span().get_span_context().trace_id == turno.get_span_context().trace_id
    celery_trazas.al_terminar(task_id="t-1")
    assert not trace.get_current_span().get_span_context().is_valid, "el contexto se suelta"

    [salida, _] = sorted(exportador.get_finished_spans(), key=lambda s: s.name)
    assert salida.name == "celery.core.outbound_send"
    assert salida.context.trace_id == turno.get_span_context().trace_id
    assert salida.parent is not None and salida.parent.span_id == turno.get_span_context().span_id


def test_sin_traza_la_tarea_corre_igual() -> None:
    from core.observability import celery_trazas

    celery_trazas.al_publicar(headers=None)
    celery_trazas.al_empezar(
        task_id="t-2", task=SimpleNamespace(name="x", request=SimpleNamespace())
    )
    celery_trazas.al_terminar(task_id="t-2")
    celery_trazas.al_terminar(task_id="desconocida")


def test_veredicto_para_turno_solo_codigos_y_conteos() -> None:
    from core.orchestrator.verificador.verificador import Veredicto, veredicto_para_turno

    v = Veredicto(
        texto="x",
        conteo={"montos": 2, "links": 0},
        urls_quitadas=1,
        sombra={"referencias_sin_evidencia": 1},
    )
    salida = veredicto_para_turno(v, "reintento", ["monto_sin_evidencia"])
    assert salida == [
        {"kind": "montos", "verdict": "pass", "count": 2},
        {"kind": "links_sin_respaldo", "verdict": "fail", "count": 1},
        {"kind": "monto_sin_evidencia", "verdict": "fail", "count": 1},
        {"kind": "referencias_sin_evidencia", "verdict": "unverifiable", "count": 1},
    ]
    assert veredicto_para_turno(Veredicto(texto="", error=True), "error", []) == [
        {"kind": "verificador", "verdict": "error", "count": 1}
    ]


async def test_un_turno_de_otro_negocio_anidado_abre_el_suyo_y_no_anota_en_el_ajeno() -> None:
    vistos: dict[str, decisions.Turno | None] = {}

    @decisions.en_turno(anota_resultado=True)
    async def de_b() -> SimpleNamespace:
        vistos["b"] = decisions.turno_actual()
        decisions.anotar_uso(100, 50, 0.5)
        return SimpleNamespace(text="", escalated=True)

    @decisions.en_turno
    async def de_a() -> None:
        vistos["a"] = decisions.turno_actual()
        async with tenant_scope(T2):
            assert decisions.turno_actual() is None, "el turno de A no es visible bajo B"
            await de_b()
        vistos["a_despues"] = decisions.turno_actual()

    async with tenant_scope(T1):
        await de_a()
    a, b = vistos["a"], vistos["b"]
    assert a is not None and b is not None and a is not b
    assert b.tenant_id == T2 and b.status == "escalated" and b.tokens_in == 100
    assert a.tokens_in == 0 and a.status is None and vistos["a_despues"] is a


async def test_la_excepcion_no_queda_en_el_span_del_turno(monkeypatch) -> None:
    exportador = InMemorySpanExporter()
    proveedor = TracerProvider()
    proveedor.add_span_processor(SimpleSpanProcessor(exportador))
    monkeypatch.setattr(decisions, "get_tracer", lambda: proveedor.get_tracer("test"))

    @decisions.en_turno
    async def f() -> None:
        raise RuntimeError("dato del cliente: 3001234567")

    async with tenant_scope(T1):
        with pytest.raises(RuntimeError):
            await f()
    [span] = exportador.get_finished_spans()
    assert span.events == () or all("3001234567" not in str(e.attributes) for e in span.events)
    assert span.attributes is not None and span.attributes["error.type"] == "RuntimeError"
