"""API de gestión de crons (ADR-0183), sin base: roles y mapeo de errores del servicio.

El flujo real contra Postgres y Redis está en `tests/integration/test_crons_gestion.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.admin import crons_service as svc
from core.api import deps
from core.api.errors import ApiError, api_error_handler, validation_error_handler
from core.api.routers import crons as router_mod
from core.db.models import Admin

BASE = "/api/v1/admin/platform/crons"
NOMBRE = "core.billing_renovacion"
AHORA = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _vista(**cambios) -> svc.CronView:
    datos = dict(
        name=NOMBRE, module="Facturación de la plataforma", label="Renovación del plan", description="x",
        default_schedule="0 13 * * *", interval_s=None, schedule="0 13 * * *", overridden=False,
        paused=False, critical=False, sensitive=True, reprogramable=True, record_success=True,
        estado="ok", next_run_at=AHORA, last_run=None, last_success_at=None, runs_24h=0,
        failures_24h=0, acciones=["run", "pause", "schedule"],
    )
    datos.update(cambios)
    return svc.CronView(**datos)


def _cliente(monkeypatch, rol: str) -> TestClient:
    async def principal():
        admin = Admin(username="x", email="x@x.test", password_hash="x", role=rol, active=True)
        admin.id = uuid.uuid4()
        yield deps.Principal(admin=admin, tenant_id=None, tenants=[], permissions={})

    app = FastAPI()
    app.add_exception_handler(ApiError, api_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(  # type: ignore[arg-type]
        __import__("fastapi.exceptions", fromlist=["RequestValidationError"]).RequestValidationError,
        validation_error_handler,
    )
    app.include_router(router_mod.router, prefix="/api/v1")
    app.dependency_overrides[deps.get_principal] = principal
    return TestClient(app)


@pytest.fixture
def master(monkeypatch):
    return _cliente(monkeypatch, "master")


@pytest.mark.parametrize("rol", ["owner", "colaborador", "tester"])
@pytest.mark.parametrize(
    ("metodo", "ruta", "cuerpo"),
    [
        ("get", "", None),
        ("get", "/summary", None),
        ("get", f"/{NOMBRE}", None),
        ("get", f"/{NOMBRE}/runs", None),
        ("post", f"/{NOMBRE}/run", None),
        ("post", f"/{NOMBRE}/pause", None),
        ("post", f"/{NOMBRE}/resume", None),
        ("put", f"/{NOMBRE}/schedule", {"schedule": "0 5 * * *"}),
        ("delete", f"/{NOMBRE}/schedule", None),
    ],
)
def test_solo_el_master_incluso_en_test_con_testers_habilitados(monkeypatch, rol, metodo, ruta, cuerpo):
    monkeypatch.setenv("QA_TESTERS_ENABLED", "true")
    from core.config import get_settings

    get_settings.cache_clear()
    try:
        r = _cliente(monkeypatch, rol).request(metodo, BASE + ruta, json=cuerpo)
    finally:
        get_settings.cache_clear()
    assert r.status_code == 403, f"{rol} {metodo} {ruta}: {r.text}"


def test_listado_con_filtros(monkeypatch, master):
    visto = {}

    async def listar(module=None, estado=None, q=None, ahora=None):
        visto.update(module=module, estado=estado, q=q)
        return [_vista()]

    monkeypatch.setattr(svc, "listar", listar)
    r = master.get(BASE, params={"module": "Cobranza", "estado": "failing", "q": "mora"})
    assert r.status_code == 200
    assert visto == {"module": "Cobranza", "estado": "failing", "q": "mora"}
    cuerpo = r.json()
    assert cuerpo["items"][0]["name"] == NOMBRE and cuerpo["items"][0]["acciones"] == ["run", "pause", "schedule"]
    assert cuerpo["modules"][0] == "Canales y Meta"


def test_estado_invalido_es_422(master):
    assert master.get(BASE, params={"estado": "inventado"}).status_code == 422


def test_resumen(monkeypatch, master):
    async def resumen(ahora=None):
        return svc.Resumen(
            total=3, activos=2, pausados=1, con_fallo=1, ejecutando=0,
            modulos=[{"module": "Cobranza", "total": 3, "pausados": 1, "con_fallo": 1}],
        )

    monkeypatch.setattr(svc, "resumen", resumen)
    r = master.get(BASE + "/summary")
    assert r.status_code == 200 and r.json()["modulos"][0]["module"] == "Cobranza"


def test_cron_inexistente_es_404(monkeypatch, master):
    async def detalle(name, ahora=None):
        raise svc.CronNotFound(name)

    monkeypatch.setattr(svc, "detalle", detalle)
    assert master.get(BASE + "/core.no_existe").status_code == 404


def test_historial_paginado(monkeypatch, master):
    async def ejecuciones(name, offset, limit, status=None, trigger=None, ahora=None):
        assert (offset, limit, status, trigger) == (20, 20, "failed", "manual")
        fila = svc.UltimaEjecucion(uuid.uuid4(), "failed", "manual", AHORA, AHORA, 12, "KeyError", None)
        return [fila], 41

    monkeypatch.setattr(svc, "ejecuciones", ejecuciones)
    r = master.get(f"{BASE}/{NOMBRE}/runs", params={"page": 2, "status": "failed", "trigger": "manual"})
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["total"] == 41 and cuerpo["has_more"] is True
    assert cuerpo["items"][0]["error_class"] == "KeyError"


def test_ejecutar_ahora_responde_202_con_el_admin(monkeypatch, master):
    llamado = {}

    async def ejecutar(name, admin_id):
        llamado.update(name=name, admin_id=admin_id)

    monkeypatch.setattr(svc, "ejecutar_ahora", ejecutar)
    r = master.post(f"{BASE}/{NOMBRE}/run")
    assert r.status_code == 202 and r.json()["queued"] is True
    assert llamado["name"] == NOMBRE and isinstance(llamado["admin_id"], uuid.UUID)


@pytest.mark.parametrize(
    ("error", "status", "codigo"),
    [
        (svc.CronConflict("ya corre"), 409, "cron_conflict"),
        (svc.EnqueueFailed("sin broker"), 502, "enqueue_failed"),
        (svc.CronNotFound("x"), 404, None),
    ],
)
def test_errores_del_servicio_se_traducen(monkeypatch, master, error, status, codigo):
    async def ejecutar(name, admin_id):
        raise error

    monkeypatch.setattr(svc, "ejecutar_ahora", ejecutar)
    r = master.post(f"{BASE}/{NOMBRE}/run")
    assert r.status_code == status
    if codigo:
        assert r.json()["error"]["code"] == codigo if "error" in r.json() else codigo in r.text


def test_pausar_y_reanudar_devuelven_el_cron(monkeypatch, master):
    async def pausar(name, admin_id):
        return _vista(paused=True, estado="paused", acciones=["run", "resume", "schedule"])

    monkeypatch.setattr(svc, "pausar", pausar)
    r = master.post(f"{BASE}/{NOMBRE}/pause")
    assert r.status_code == 200 and r.json()["paused"] is True


def test_pausar_un_cron_critico_es_409(monkeypatch, master):
    async def pausar(name, admin_id):
        raise svc.CronConflict("Este cron es crítico para la plataforma: no se puede pausar.")

    monkeypatch.setattr(svc, "pausar", pausar)
    assert master.post(f"{BASE}/core.worker_heartbeat/pause").status_code == 409


def test_reprogramar_valida_y_traduce(monkeypatch, master):
    async def reprogramar(name, expresion, admin_id):
        if expresion == "nada nada":
            raise svc.CronInvalid("La expresión debe tener 5 campos.")
        return _vista(schedule=expresion, overridden=True)

    monkeypatch.setattr(svc, "reprogramar", reprogramar)
    ok = master.put(f"{BASE}/{NOMBRE}/schedule", json={"schedule": "0 5 * * *"})
    assert ok.status_code == 200 and ok.json()["schedule"] == "0 5 * * *" and ok.json()["overridden"] is True
    malo = master.put(f"{BASE}/{NOMBRE}/schedule", json={"schedule": "nada nada"})
    assert malo.status_code == 400
    # Todo texto malo pasa por el mismo validador (400 con mensaje), no por el esquema.
    vacio = master.put(f"{BASE}/{NOMBRE}/schedule", json={"schedule": ""})
    assert vacio.status_code == 422
