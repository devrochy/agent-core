"""Gestión de crons (ADR-0183) contra Postgres y Redis reales.

`/api/v1/admin/platform/crons`:

- Catálogo completo (todos los crons del beat) sin historial: `never`/`untracked`.
- Pausar y reanudar; un cron crítico no se pausa (409); sin cron → 404.
- Reprogramar valida la expresión (400), rechaza los de intervalo fijo (409), restaura el
  horario por defecto, y una expresión igual al defecto no deja un override.
- Ejecución manual: encola con `trigger=manual` y el admin; con el lock tomado, 409.
- Historial: estado `failing`, últimas ejecuciones, conteos de 24 h, paginación y filtros; una
  ejecución colgada se ve como fallida (`Interrumpida`) y el mantenimiento la cierra y poda a los
  30 días.

El wrapper `run_cron` y el planificador (que usan el loop persistente de Celery) se prueban sin
base en `test_cron_runtime.py` / `test_cron_scheduler.py` y de punta a punta con un worker y un
beat reales (ver la PR).
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest
from _api_helpers import api_headers
from sqlalchemy import text

from core.admin import crons_service as svc
from core.db.models import utcnow
from core.tasks.cron_catalog import CATALOGO

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

BASE = "/api/v1/admin/platform/crons"
T1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
COBRO = "core.billing_renovacion"  # sensible, reprogramable, registra sus éxitos
HEARTBEAT = "core.worker_heartbeat"  # crítico, sin historial de éxitos
OUTBOX = "core.domain_events_dispatch"  # intervalo fijo


async def _sql(sql: str, **params) -> None:
    from core.db.session import get_session

    async with get_session() as session:
        await session.execute(text(sql), params)


@pytest.fixture(autouse=True)
async def _limpio():
    await _sql("delete from cron_runs")
    await _sql("delete from cron_job_configs")
    from core.memory.working import get_redis

    await get_redis().delete(*[svc.LOCK_PREFIX + n for n in CATALOGO])
    yield
    # No dejar crons pausados ni reprogramados a los tests que corren después.
    await _sql("delete from cron_runs")
    await _sql("delete from cron_job_configs")
    await get_redis().delete(*[svc.LOCK_PREFIX + n for n in CATALOGO])


@pytest.fixture
async def h(app_client):
    return await api_headers(app_client, T1)


async def test_el_catalogo_trae_todos_los_crons_sin_historial(app_client, h):
    r = await app_client.get(BASE, headers=h)
    assert r.status_code == 200, r.text
    items = {i["name"]: i for i in r.json()["items"]}
    assert set(items) == set(CATALOGO)
    assert items[COBRO]["estado"] == "never" and items[HEARTBEAT]["estado"] == "untracked"
    assert items[COBRO]["next_run_at"] is not None and items[OUTBOX]["next_run_at"] is None
    assert items[OUTBOX]["interval_s"] == 30.0 and items[OUTBOX]["acciones"] == ["run"]
    assert items[HEARTBEAT]["acciones"] == ["run"]  # crítico: sin pausa; no reprogramable
    assert items[COBRO]["acciones"] == ["run", "pause", "schedule"]
    assert items[COBRO]["sensitive"] is True


async def test_filtros_del_listado(app_client, h):
    por_modulo = (await app_client.get(BASE, headers=h, params={"module": "Cobranza"})).json()["items"]
    assert por_modulo and {i["module"] for i in por_modulo} == {"Cobranza"}
    por_texto = (await app_client.get(BASE, headers=h, params={"q": "RENOVACION"})).json()["items"]
    assert [i["name"] for i in por_texto] == [COBRO]
    await app_client.post(f"{BASE}/{COBRO}/pause", headers=h)
    pausados = (await app_client.get(BASE, headers=h, params={"estado": "paused"})).json()["items"]
    assert [i["name"] for i in pausados] == [COBRO]


async def test_resumen_por_modulo(app_client, h):
    await app_client.post(f"{BASE}/{COBRO}/pause", headers=h)
    r = (await app_client.get(f"{BASE}/summary", headers=h)).json()
    assert r["total"] == len(CATALOGO) and r["pausados"] == 1 and r["activos"] == len(CATALOGO) - 1
    facturacion = next(m for m in r["modulos"] if m["module"] == "Facturación de la plataforma")
    assert facturacion["pausados"] == 1
    assert sum(m["total"] for m in r["modulos"]) == len(CATALOGO)


async def test_pausar_y_reanudar(app_client, h):
    pausado = (await app_client.post(f"{BASE}/{COBRO}/pause", headers=h)).json()
    assert pausado["paused"] is True and pausado["estado"] == "paused" and pausado["next_run_at"] is None
    assert pausado["acciones"] == ["run", "resume", "schedule"]
    assert await svc.esta_pausado(COBRO) is True
    reanudado = (await app_client.post(f"{BASE}/{COBRO}/resume", headers=h)).json()
    assert reanudado["paused"] is False and reanudado["estado"] == "never" and reanudado["next_run_at"]
    assert await svc.esta_pausado(COBRO) is False


async def test_un_cron_critico_no_se_pausa_y_uno_inexistente_es_404(app_client, h):
    assert (await app_client.post(f"{BASE}/{HEARTBEAT}/pause", headers=h)).status_code == 409
    assert (await app_client.post(f"{BASE}/core.no_existe/pause", headers=h)).status_code == 404
    assert (await app_client.get(f"{BASE}/core.no_existe", headers=h)).status_code == 404
    assert (await app_client.get(f"{BASE}/core.no_existe/runs", headers=h)).status_code == 404


async def test_reprogramar_y_restaurar(app_client, h):
    r = await app_client.put(f"{BASE}/{COBRO}/schedule", headers=h, json={"schedule": "  0   5 * * *"})
    assert r.status_code == 200, r.text
    cron = r.json()
    assert cron["schedule"] == "0 5 * * *" and cron["overridden"] is True
    assert cron["default_schedule"] == "0 13 * * *"
    assert cron["acciones"] == ["run", "pause", "schedule", "reset_schedule"]
    assert (await svc.cargar_config())[COBRO].schedule_override == "0 5 * * *"
    assert "T05:00:00" in cron["next_run_at"]

    restaurado = (await app_client.delete(f"{BASE}/{COBRO}/schedule", headers=h)).json()
    assert restaurado["overridden"] is False and restaurado["schedule"] == "0 13 * * *"
    assert (await svc.cargar_config())[COBRO].schedule_override is None


async def test_reprogramar_al_horario_por_defecto_no_deja_override(app_client, h):
    await app_client.put(f"{BASE}/{COBRO}/schedule", headers=h, json={"schedule": "0 5 * * *"})
    r = (await app_client.put(f"{BASE}/{COBRO}/schedule", headers=h, json={"schedule": "0 13 * * *"})).json()
    assert r["overridden"] is False
    assert (await svc.cargar_config())[COBRO].schedule_override is None


@pytest.mark.parametrize("malo", ["* * * *", "61 * * * *", "0 4 * * MON", "abc def ghi jkl mno", "@daily  x"])
async def test_expresion_invalida_es_400_y_no_cambia_nada(app_client, h, malo):
    r = await app_client.put(f"{BASE}/{COBRO}/schedule", headers=h, json={"schedule": malo})
    assert r.status_code == 400, r.text
    assert COBRO not in await svc.cargar_config()


async def test_un_cron_de_intervalo_fijo_no_se_reprograma(app_client, h):
    r = await app_client.put(f"{BASE}/{OUTBOX}/schedule", headers=h, json={"schedule": "* * * * *"})
    assert r.status_code == 409
    assert OUTBOX not in await svc.cargar_config()


async def test_pausa_y_horario_son_independientes(app_client, h):
    await app_client.put(f"{BASE}/{COBRO}/schedule", headers=h, json={"schedule": "0 5 * * *"})
    await app_client.post(f"{BASE}/{COBRO}/pause", headers=h)
    cfg = (await svc.cargar_config())[COBRO]
    assert cfg.paused is True and cfg.schedule_override == "0 5 * * *"
    await app_client.delete(f"{BASE}/{COBRO}/schedule", headers=h)
    cfg = (await svc.cargar_config())[COBRO]
    assert cfg.paused is True and cfg.schedule_override is None


async def test_ejecucion_manual_encola_con_el_admin(app_client, h, monkeypatch):
    from core.tasks.celery_app import celery

    enviados = []
    monkeypatch.setattr(celery, "send_task", lambda nombre, **kw: enviados.append((nombre, kw)))
    r = await app_client.post(f"{BASE}/{COBRO}/run", headers=h)
    assert r.status_code == 202, r.text
    assert len(enviados) == 1 and enviados[0][0] == COBRO
    kwargs = enviados[0][1]["kwargs"]
    assert kwargs["trigger"] == "manual" and uuid.UUID(kwargs["triggered_by"])


async def test_ejecucion_manual_de_un_cron_pausado_se_permite(app_client, h, monkeypatch):
    from core.tasks.celery_app import celery

    enviados = []
    monkeypatch.setattr(celery, "send_task", lambda nombre, **kw: enviados.append(nombre))
    await app_client.post(f"{BASE}/{COBRO}/pause", headers=h)
    assert (await app_client.post(f"{BASE}/{COBRO}/run", headers=h)).status_code == 202
    assert enviados == [COBRO]


async def test_ejecucion_manual_con_el_lock_tomado_es_409(app_client, h, monkeypatch):
    from core.tasks.celery_app import celery

    enviados = []
    monkeypatch.setattr(celery, "send_task", lambda nombre, **kw: enviados.append(nombre))
    assert await svc.tomar_lock(COBRO) is True
    assert await svc.tomar_lock(COBRO) is False  # una sola ejecución a la vez
    r = await app_client.post(f"{BASE}/{COBRO}/run", headers=h)
    assert r.status_code == 409 and enviados == []
    await svc.soltar_lock(COBRO)
    assert (await app_client.post(f"{BASE}/{COBRO}/run", headers=h)).status_code == 202


async def test_si_el_broker_falla_es_502(app_client, h, monkeypatch):
    from core.tasks.celery_app import celery

    def roto(*_a, **_k):
        raise ConnectionError("broker caído")

    monkeypatch.setattr(celery, "send_task", roto)
    assert (await app_client.post(f"{BASE}/{COBRO}/run", headers=h)).status_code == 502


async def test_historial_estado_y_conteos(app_client, h):
    admin = uuid.uuid4()
    r1 = await svc.iniciar_ejecucion(COBRO, "scheduled", None)
    await svc.cerrar_ejecucion(r1, "success", 120)
    r2 = await svc.iniciar_ejecucion(COBRO, "manual", admin)
    await svc.cerrar_ejecucion(r2, "failed", 40, "KeyError")
    await svc.registrar_ejecucion(COBRO, "skipped", "scheduled", None, utcnow(), 0, detail="plugin_inactivo")

    cron = (await app_client.get(f"{BASE}/{COBRO}", headers=h)).json()
    assert cron["runs_24h"] == 3 and cron["failures_24h"] == 1
    assert cron["last_success_at"] is not None
    assert cron["last_run"]["status"] == "skipped" and cron["last_run"]["detail"] == "plugin_inactivo"
    assert cron["estado"] == "ok"  # lo último fue una omisión, no un fallo

    r3 = await svc.iniciar_ejecucion(COBRO, "scheduled", None)
    await svc.cerrar_ejecucion(r3, "failed", 5, "TimeoutError")
    cron = (await app_client.get(f"{BASE}/{COBRO}", headers=h)).json()
    assert cron["estado"] == "failing" and cron["last_run"]["error_class"] == "TimeoutError"

    resumen = (await app_client.get(f"{BASE}/summary", headers=h)).json()
    assert resumen["con_fallo"] == 1


async def test_historial_paginado_y_filtrado(app_client, h):
    for i in range(5):
        run = await svc.iniciar_ejecucion(COBRO, "manual" if i % 2 else "scheduled", None)
        await svc.cerrar_ejecucion(run, "failed" if i == 3 else "success", i)
    todo = (await app_client.get(f"{BASE}/{COBRO}/runs", headers=h, params={"page_size": 2})).json()
    assert todo["total"] == 5 and len(todo["items"]) == 2 and todo["has_more"] is True
    fechas = [i["started_at"] for i in todo["items"]]
    assert fechas == sorted(fechas, reverse=True)  # lo más nuevo primero
    fallidas = (await app_client.get(f"{BASE}/{COBRO}/runs", headers=h, params={"status": "failed"})).json()
    assert fallidas["total"] == 1
    manuales = (await app_client.get(f"{BASE}/{COBRO}/runs", headers=h, params={"trigger": "manual"})).json()
    assert manuales["total"] == 2
    otro = (await app_client.get(f"{BASE}/core.billing_lifecycle/runs", headers=h)).json()
    assert otro["total"] == 0  # el historial es de cada cron


async def test_una_ejecucion_colgada_se_ve_fallida_y_el_mantenimiento_la_cierra(app_client, h):
    viejo = utcnow() - timedelta(minutes=30)
    await _sql(
        "insert into cron_runs (id, task_name, status, trigger, started_at) "
        "values (:i, :n, 'running', 'scheduled', :s)",
        i=uuid.uuid4(), n=COBRO, s=viejo,
    )
    cron = (await app_client.get(f"{BASE}/{COBRO}", headers=h)).json()
    assert cron["estado"] == "failing"
    assert cron["last_run"]["status"] == "failed" and cron["last_run"]["error_class"] == "Interrumpida"

    # Una en curso y reciente sigue en curso.
    await svc.iniciar_ejecucion("core.billing_lifecycle", "scheduled", None)
    en_curso = (await app_client.get(f"{BASE}/core.billing_lifecycle", headers=h)).json()
    assert en_curso["estado"] == "running"

    resultado = await svc.mantenimiento()
    assert resultado["colgadas"] == 1 and resultado["borradas"] == 0
    cerrada = (await app_client.get(f"{BASE}/{COBRO}/runs", headers=h)).json()["items"][0]
    assert cerrada["status"] == "failed" and cerrada["error_class"] == "Interrumpida"


async def test_el_mantenimiento_poda_a_los_30_dias(app_client, h):
    for dias in (45, 31, 29, 1):
        await _sql(
            "insert into cron_runs (id, task_name, status, trigger, started_at, finished_at) "
            "values (:i, :n, 'success', 'scheduled', :s, :s)",
            i=uuid.uuid4(), n=COBRO, s=utcnow() - timedelta(days=dias),
        )
    assert (await svc.mantenimiento())["borradas"] == 2
    assert (await app_client.get(f"{BASE}/{COBRO}/runs", headers=h)).json()["total"] == 2


async def test_sin_sesion_es_401(app_client):
    """Los roles (dueño, colaborador, tester → 403) se prueban sin base en `test_crons_api.py`."""
    assert (await app_client.get(BASE)).status_code == 401
    assert (await app_client.post(f"{BASE}/{COBRO}/run")).status_code == 401
