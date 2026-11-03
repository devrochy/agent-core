"""`run_cron` (ADR-0183): pausa, lock anti-solape e historial, sin base ni Redis (el servicio se
reemplaza por un doble en memoria)."""

from __future__ import annotations

import uuid

from core.admin import crons_service as svc
from core.tasks import cron_runtime
from core.tasks.cron_runtime import OMITIDA_EN_EJECUCION, OMITIDA_PLUGIN_INACTIVO, run_cron

UN_CRON = "core.billing_conciliacion"  # registra sus éxitos
SIN_HISTORIAL = "core.worker_heartbeat"  # record_success=False


class Doble:
    """Reemplaza las funciones del servicio y registra lo que el wrapper les pidió."""

    def __init__(self, monkeypatch, *, pausado=False, lock=True, falla_registro=False):
        self.filas: list[dict] = []
        self.pausado, self.lock, self.falla_registro = pausado, lock, falla_registro
        self.locks_soltados = 0
        monkeypatch.setattr(svc, "esta_pausado", self._pausado)
        monkeypatch.setattr(svc, "tomar_lock", self._tomar)
        monkeypatch.setattr(svc, "soltar_lock", self._soltar)
        monkeypatch.setattr(svc, "iniciar_ejecucion", self._iniciar)
        monkeypatch.setattr(svc, "cerrar_ejecucion", self._cerrar)
        monkeypatch.setattr(svc, "registrar_ejecucion", self._registrar)

    async def _pausado(self, _n):
        return self.pausado

    async def _tomar(self, _n):
        return self.lock

    async def _soltar(self, _n):
        self.locks_soltados += 1

    async def _iniciar(self, n, trigger, quien):
        if self.falla_registro:
            raise ConnectionError("base caída")
        fila = {"task": n, "status": "running", "trigger": trigger, "by": quien}
        self.filas.append(fila)
        return len(self.filas) - 1

    async def _cerrar(self, run_id, status, duracion, error_class=None):
        self.filas[run_id].update(status=status, error_class=error_class, ms=duracion)

    async def _registrar(self, n, status, trigger, quien, inicio, duracion, error_class=None, detail=None):
        self.filas.append(
            {"task": n, "status": status, "trigger": trigger, "by": quien, "error_class": error_class, "detail": detail}
        )


def _job_ok(llamadas: list):
    async def job():
        llamadas.append(1)

    return job


def test_exito_queda_registrado_y_suelta_el_lock(monkeypatch):
    d, llamadas = Doble(monkeypatch), []
    run_cron(UN_CRON, _job_ok(llamadas))
    assert llamadas == [1]
    assert [(f["status"], f["trigger"]) for f in d.filas] == [("success", "scheduled")]
    assert d.filas[0]["error_class"] is None
    assert d.locks_soltados == 1


def test_un_fallo_se_registra_con_la_clase_y_no_se_relanza(monkeypatch):
    d = Doble(monkeypatch)

    async def job():
        raise KeyError("dato-de-un-negocio")

    run_cron(UN_CRON, job)  # no lanza
    assert d.filas[0]["status"] == "failed"
    assert d.filas[0]["error_class"] == "KeyError"
    assert "dato-de-un-negocio" not in str(d.filas)  # nunca el mensaje
    assert d.locks_soltados == 1


def test_un_job_sincrono_funciona(monkeypatch):
    d, llamadas = Doble(monkeypatch), []
    run_cron(UN_CRON, lambda: llamadas.append(1))
    assert llamadas == [1] and d.filas[0]["status"] == "success"


def test_pausado_no_corre_ni_deja_fila(monkeypatch):
    d, llamadas = Doble(monkeypatch, pausado=True), []
    run_cron(UN_CRON, _job_ok(llamadas))
    assert llamadas == [] and d.filas == [] and d.locks_soltados == 0


def test_la_ejecucion_manual_ignora_la_pausa_y_guarda_quien_fue(monkeypatch):
    d, llamadas = Doble(monkeypatch, pausado=True), []
    quien = uuid.uuid4()
    run_cron(UN_CRON, _job_ok(llamadas), trigger="manual", triggered_by=str(quien))
    assert llamadas == [1]
    assert d.filas[0]["trigger"] == "manual" and d.filas[0]["by"] == quien


def test_un_triggered_by_invalido_no_rompe(monkeypatch):
    d = Doble(monkeypatch)
    run_cron(UN_CRON, _job_ok([]), trigger="manual", triggered_by="no-es-un-uuid")
    assert d.filas[0]["by"] is None


def test_si_ya_corre_se_omite_sin_ejecutar(monkeypatch):
    d, llamadas = Doble(monkeypatch, lock=False), []
    run_cron(UN_CRON, _job_ok(llamadas))
    assert llamadas == []
    assert d.filas[0]["status"] == "skipped" and d.filas[0]["detail"] == OMITIDA_EN_EJECUCION
    assert d.locks_soltados == 0  # el lock es de la otra ejecución: no se suelta


def test_un_pack_inactivo_se_omite_y_suelta_el_lock(monkeypatch):
    d, llamadas = Doble(monkeypatch), []
    run_cron(UN_CRON, _job_ok(llamadas), skip_if=lambda: OMITIDA_PLUGIN_INACTIVO)
    assert llamadas == []
    assert d.filas[0]["status"] == "skipped" and d.filas[0]["detail"] == OMITIDA_PLUGIN_INACTIVO
    assert d.locks_soltados == 1


def test_sin_historial_de_exitos_no_guarda_nada(monkeypatch):
    d, llamadas = Doble(monkeypatch), []
    run_cron(SIN_HISTORIAL, _job_ok(llamadas))
    assert llamadas == [1] and d.filas == []


def test_sin_historial_de_exitos_igual_guarda_el_fallo(monkeypatch):
    d = Doble(monkeypatch)

    async def job():
        raise RuntimeError

    run_cron(SIN_HISTORIAL, job)
    assert [(f["status"], f["error_class"]) for f in d.filas] == [("failed", "RuntimeError")]


def test_sin_historial_de_exitos_guarda_la_ejecucion_manual(monkeypatch):
    d = Doble(monkeypatch)
    run_cron(SIN_HISTORIAL, _job_ok([]), trigger="manual")
    assert [(f["status"], f["trigger"]) for f in d.filas] == [("success", "manual")]


def test_si_la_base_falla_el_cron_corre_igual(monkeypatch):
    d, llamadas = Doble(monkeypatch, falla_registro=True), []
    run_cron(UN_CRON, _job_ok(llamadas))
    assert llamadas == [1]
    assert d.locks_soltados == 1


def test_si_redis_falla_el_cron_corre_igual(monkeypatch):
    Doble(monkeypatch)
    llamadas = []

    async def sin_redis(_n):
        raise ConnectionError("redis caído")

    monkeypatch.setattr(svc, "tomar_lock", sin_redis)
    run_cron(UN_CRON, _job_ok(llamadas))
    assert llamadas == [1]


def test_un_cron_desconocido_se_trata_como_registrable(monkeypatch):
    d = Doble(monkeypatch)
    run_cron("pack_inexistente.algo", _job_ok([]))
    assert d.filas[0]["status"] == "success"


def test_register_cron_pasa_por_run_cron_y_acepta_los_kwargs_manuales(monkeypatch):
    """El `@register_cron` de un pack envuelve su función con `run_cron` (pausa, lock e historial)."""
    from core.tasks.celery_app import celery
    from core.tasks.decorators import register_cron

    d, llamadas = Doble(monkeypatch), []

    @register_cron("pack_cron_prueba_adr0183", "0 3 * * *")
    async def _job() -> None:
        llamadas.append(1)

    class _PM:
        async def ensure_process_activation(self):
            return None

        def is_active(self, _plugin_id):
            return True

    import core.plugin_manager as pm

    monkeypatch.setattr(pm, "plugin_manager", _PM())
    try:
        tarea = celery.tasks["pack_cron_prueba_adr0183._job"]
        tarea.run()  # lo que hace el beat: sin argumentos
        tarea.run(trigger="manual", triggered_by=str(uuid.uuid4()))  # lo que hace POST /run
        assert llamadas == [1, 1]
        assert [f["trigger"] for f in d.filas] == ["scheduled", "manual"]
    finally:
        celery.conf.beat_schedule.pop("pack_cron_prueba_adr0183._job", None)


def test_modulo_exporta_los_motivos():
    assert cron_runtime.OMITIDA_EN_EJECUCION == "en_ejecucion"
    assert cron_runtime.OMITIDA_PLUGIN_INACTIVO == "plugin_inactivo"
