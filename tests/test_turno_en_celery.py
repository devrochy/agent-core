"""`turn_id` dentro de las tareas Celery (seguimiento #439 de HU-AOS-10), sin base de datos.

- Al encolar dentro de un turno, el turno viaja en headers del mensaje, con la guarda de
  negocio del default de columna; un turno heredado (worker) no se propaga a tareas nuevas.
- En el worker, `task_prerun` fija el turno (o lo limpia) y `task_postrun` lo suelta.
- `run_async` lleva el ContextVar del hilo de la tarea a la corrutina.
"""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from core.observability import celery_trazas
from core.shared import turno as turno_vigente
from core.shared.async_bridge import run_async
from core.shared.tenant import tenant_scope

T1 = uuid.UUID("30000000-0000-0000-0000-000000000001")
T2 = uuid.UUID("30000000-0000-0000-0000-000000000002")
TRAZA = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def turno():
    t = turno_vigente.TurnoVigente(turn_id=uuid.uuid4(), tenant_id=T1, trace_id=TRAZA)
    token = turno_vigente.fijar(t)
    yield t
    turno_vigente.soltar(token)


def _tarea(headers: dict[str, Any]) -> SimpleNamespace:
    """Como la ve el worker: Celery copia los headers del mensaje en `task.request`."""
    return SimpleNamespace(name="core.outbound_send", request=SimpleNamespace(**headers))


async def test_dentro_del_turno_el_mensaje_lleva_el_turno(turno) -> None:
    headers: dict[str, Any] = {"id": "tarea-1"}
    async with tenant_scope(T1):
        celery_trazas.turno_al_publicar(headers=headers)
    assert headers[celery_trazas.H_TURNO] == str(turno.turn_id)
    assert headers[celery_trazas.H_NEGOCIO] == str(T1)
    assert headers[celery_trazas.H_TRAZA] == TRAZA


async def test_bajo_otro_negocio_o_sin_negocio_no_viaja(turno) -> None:
    ajenos: dict[str, Any] = {}
    async with tenant_scope(T2):
        celery_trazas.turno_al_publicar(headers=ajenos)
    sin_scope: dict[str, Any] = {}
    celery_trazas.turno_al_publicar(headers=sin_scope)
    assert ajenos == {} and sin_scope == {}


async def test_fuera_de_un_turno_no_viaja() -> None:
    headers: dict[str, Any] = {}
    async with tenant_scope(T1):
        celery_trazas.turno_al_publicar(headers=headers)
        celery_trazas.turno_al_publicar(headers=None)
    assert headers == {}


async def test_no_pisa_los_headers_de_un_reintento(turno) -> None:
    previo = {celery_trazas.H_TURNO: "otro", celery_trazas.H_NEGOCIO: str(T1)}
    headers = dict(previo)
    async with tenant_scope(T1):
        celery_trazas.turno_al_publicar(headers=headers)
    assert headers == previo


def test_el_worker_fija_el_turno_de_la_tarea_y_lo_suelta() -> None:
    turn_id = uuid.uuid4()
    headers = {
        celery_trazas.H_TURNO: str(turn_id),
        celery_trazas.H_NEGOCIO: str(T1),
        celery_trazas.H_TRAZA: TRAZA,
    }
    celery_trazas.turno_al_empezar(task_id="w-1", task=_tarea(headers))
    try:
        fijado = turno_vigente._vigente.get()
        assert fijado == turno_vigente.TurnoVigente(
            turn_id=turn_id, tenant_id=T1, trace_id=TRAZA, heredado=True
        )
        fila = SimpleNamespace(get_current_parameters=lambda: {"tenant_id": T1})
        ajena = SimpleNamespace(get_current_parameters=lambda: {"tenant_id": T2})
        assert turno_vigente.turn_id_de_fila(fila) == turn_id
        assert turno_vigente.trace_id_de_fila(fila) == TRAZA
        assert turno_vigente.turn_id_de_fila(ajena) is None, "guarda de negocio por fila"
    finally:
        celery_trazas.turno_al_terminar(task_id="w-1")
    assert turno_vigente._vigente.get() is None


def test_los_headers_tambien_se_leen_de_request_headers() -> None:
    turn_id = uuid.uuid4()
    tarea = SimpleNamespace(
        name="x",
        request=SimpleNamespace(
            headers={celery_trazas.H_TURNO: str(turn_id), celery_trazas.H_NEGOCIO: str(T1)}
        ),
    )
    celery_trazas.turno_al_empezar(task_id="w-h", task=tarea)
    try:
        fijado = turno_vigente._vigente.get()
        assert fijado is not None and fijado.turn_id == turn_id and fijado.trace_id is None
    finally:
        celery_trazas.turno_al_terminar(task_id="w-h")


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"agentcore_turn_id": "no-es-uuid", "agentcore_turn_tenant_id": str(T1)},
        {"agentcore_turn_id": str(uuid.uuid4())},  # sin negocio
        {"agentcore_turn_id": 5, "agentcore_turn_tenant_id": str(T1)},
    ],
)
def test_sin_turno_valido_la_tarea_empieza_limpia(headers: dict[str, Any]) -> None:
    # Un turno pegado de una tarea anterior del mismo hilo (postrun perdido) se limpia.
    pegado = turno_vigente.fijar(
        turno_vigente.TurnoVigente(turn_id=uuid.uuid4(), tenant_id=T1, trace_id=None)
    )
    try:
        celery_trazas.turno_al_empezar(task_id="w-2", task=_tarea(headers))
        assert turno_vigente._vigente.get() is None
        celery_trazas.turno_al_terminar(task_id="w-2")
        assert turno_vigente._vigente.get() is not None, "postrun restaura lo anterior"
    finally:
        turno_vigente.soltar(pegado)


def test_una_traza_invalida_se_descarta_pero_el_turno_vale() -> None:
    turn_id = uuid.uuid4()
    headers = {
        celery_trazas.H_TURNO: str(turn_id),
        celery_trazas.H_NEGOCIO: str(T1),
        celery_trazas.H_TRAZA: "x" * 300,
    }
    celery_trazas.turno_al_empezar(task_id="w-3", task=_tarea(headers))
    try:
        fijado = turno_vigente._vigente.get()
        assert fijado is not None and fijado.turn_id == turn_id and fijado.trace_id is None
    finally:
        celery_trazas.turno_al_terminar(task_id="w-3")
    celery_trazas.turno_al_terminar(task_id="desconocida")


def test_de_punta_a_punta_el_turno_llega_a_la_corrutina_y_no_a_tareas_nuevas() -> None:
    """Publicar en el turno → worker (otro hilo) → `run_async`: la corrutina ve el turno. Lo
    que la tarea encola de nuevo no lo hereda (NULL antes que mal atribuido)."""
    turn_id = uuid.uuid4()
    headers: dict[str, Any] = {"id": "tarea-e2e"}

    async def _publicar() -> None:
        async with tenant_scope(T1):
            celery_trazas.turno_al_publicar(headers=headers)

    token = turno_vigente.fijar(
        turno_vigente.TurnoVigente(turn_id=turn_id, tenant_id=T1, trace_id=TRAZA)
    )
    try:
        run_async(_publicar())
    finally:
        turno_vigente.soltar(token)

    visto: dict[str, Any] = {}

    async def _cuerpo() -> None:
        async with tenant_scope(T1):
            visto["turn_id"] = turno_vigente.turno_id()
            encadenada: dict[str, Any] = {"id": "otra-tarea"}
            celery_trazas.turno_al_publicar(headers=encadenada)
            visto["encadenada"] = encadenada
        async with tenant_scope(T2):
            visto["otro_negocio"] = turno_vigente.turno_id()

    def _worker() -> None:
        celery_trazas.turno_al_empezar(task_id="tarea-e2e", task=_tarea(headers))
        try:
            run_async(_cuerpo())
        finally:
            celery_trazas.turno_al_terminar(task_id="tarea-e2e")
        visto["despues"] = run_async(_leer_vigente())

    hilo = threading.Thread(target=_worker)
    hilo.start()
    hilo.join(timeout=10)
    assert visto["turn_id"] == turn_id
    assert visto["otro_negocio"] is None
    assert visto["encadenada"] == {"id": "otra-tarea"}, "una tarea nueva no hereda el turno"
    assert visto["despues"] is None


async def _leer_vigente() -> turno_vigente.TurnoVigente | None:
    return turno_vigente._vigente.get()


def test_run_async_lleva_el_contextvar_del_hilo_llamador() -> None:
    """Supuesto del diseño: `run_coroutine_threadsafe` copia el contexto del hilo llamador
    (por eso basta con fijar el turno en el hilo de la tarea)."""
    t = turno_vigente.TurnoVigente(turn_id=uuid.uuid4(), tenant_id=T1, trace_id=None)
    visto: list[Any] = []

    def _hilo() -> None:
        token = turno_vigente.fijar(t)
        try:
            visto.append(run_async(_leer_vigente()))
        finally:
            turno_vigente.soltar(token)

    hilo = threading.Thread(target=_hilo)
    hilo.start()
    hilo.join(timeout=10)
    assert visto == [t]
