"""HU-ARCH-38: el interruptor global de la plataforma (ADR-0200).

Lector con caché y fallo cerrado, precedencia en el guardián de envío, crons que escriben a clientes, la ruta
solo para master (403 registrado) y el motivo obligatorio al activar.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.shared import kill_switch

pytestmark = pytest.mark.pausa_real


class _Sesion:
    def __init__(self, valor=None, falla: Exception | None = None) -> None:
        self.valor, self.falla, self.consultas = valor, falla, 0

    async def execute(self, *_a, **_k):
        self.consultas += 1
        if self.falla:
            raise self.falla
        return SimpleNamespace(scalar_one_or_none=lambda: self.valor)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _sin_cache():
    kill_switch.invalidar()
    yield
    kill_switch.invalidar()


def _con_sesion(monkeypatch, sesion: _Sesion) -> None:
    monkeypatch.setattr("core.db.session.get_session", lambda: sesion)

    async def _alerta(*_a, **_k):
        return None

    monkeypatch.setattr("core.shared.alerts.send_alert_once", _alerta)


async def test_lee_una_vez_y_sirve_la_cache(monkeypatch) -> None:
    sesion = _Sesion(valor=True)
    _con_sesion(monkeypatch, sesion)
    assert await kill_switch.global_activo() is True
    assert await kill_switch.motivo_global() == "kill_switch"
    assert sesion.consultas == 1, "la segunda lectura sale de la caché"
    kill_switch.invalidar()
    sesion.valor = False
    assert await kill_switch.global_activo() is False


async def test_si_no_se_puede_leer_falla_cerrado(monkeypatch) -> None:
    _con_sesion(monkeypatch, _Sesion(falla=OSError("base caída")))
    assert await kill_switch.global_activo() is True


async def test_sin_la_fila_falla_cerrado(monkeypatch) -> None:
    _con_sesion(monkeypatch, _Sesion(valor=None))
    assert await kill_switch.global_activo() is True


def test_el_ttl_es_de_pocos_segundos() -> None:
    assert kill_switch.TTL_S <= 5, "la HU pide frenar en menos de 10 s"


async def test_el_global_va_antes_que_el_negocio_y_el_cliente(monkeypatch) -> None:
    from core.gateway import pausa_automatica

    async def _activo():
        return "kill_switch"

    async def _no_deberia(*_a, **_k):
        raise AssertionError("con el global activo no se lee el negocio")

    monkeypatch.setattr(kill_switch, "motivo_global", _activo)
    monkeypatch.setattr(pausa_automatica, "_bot_paused", _no_deberia)
    conv = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4(), customer_id=uuid.uuid4())
    assert await pausa_automatica.motivo_pausa_automatica(conv) == "kill_switch"
    assert await pausa_automatica.motivo_pausa_negocio(conv.tenant_id) == "kill_switch"
    assert pausa_automatica.es_envio_automatico("human") is False, "lo de una persona sale igual"


def test_los_crons_que_escriben_a_clientes_estan_marcados_y_son_sensibles() -> None:
    from core.tasks.cron_catalog import CATALOGO

    marcados = {n for n, m in CATALOGO.items() if m.writes_to_customers}
    assert {
        "pack_commerce.follow_up_leads",
        "pack_commerce.follow_up_quotes",
        "pack_calendar.recordatorios_de_citas",
        "pack_subscriptions.avisos_de_cobro",
    } <= marcados
    assert all(CATALOGO[n].sensitive for n in marcados)
    assert not CATALOGO["pack_subscriptions.generar_cargos"].writes_to_customers, "la contabilidad sigue"


@pytest.mark.parametrize(("trigger", "corre"), [("scheduled", False), ("manual", True)])
def test_run_cron_omite_lo_programado_que_escribe_a_clientes(monkeypatch, trigger: str, corre: bool) -> None:
    from core.tasks import cron_runtime

    corridas: list[str] = []
    registradas: list[tuple] = []

    async def _activo():
        return True

    async def _no_pausado(_t):
        return False

    async def _lock(_t):
        return True

    async def _nada(*_a, **_k):
        return None

    async def _registrar(task, status, *a, **k):
        registradas.append((status, k.get("detail")))

    monkeypatch.setattr(kill_switch, "global_activo", _activo)
    monkeypatch.setattr(cron_runtime.svc, "esta_pausado", _no_pausado)
    monkeypatch.setattr(cron_runtime.svc, "tomar_lock", _lock)
    monkeypatch.setattr(cron_runtime.svc, "soltar_lock", _nada)
    monkeypatch.setattr(cron_runtime.svc, "registrar_ejecucion", _registrar)
    monkeypatch.setattr(cron_runtime.svc, "empezar_ejecucion", _nada, raising=False)
    monkeypatch.setattr(cron_runtime.svc, "terminar_ejecucion", _nada, raising=False)

    cron_runtime.run_cron("pack_commerce.follow_up_leads", lambda: corridas.append("x"), trigger=trigger)
    assert bool(corridas) is corre
    if not corre:
        assert ("skipped", "kill_switch") in registradas


async def test_activar_exige_motivo() -> None:
    from core.admin import kill_switch_service as svc

    with pytest.raises(svc.MotivoRequerido):
        await svc.fijar(True, "  ", admin_id=None, etiqueta="cli", fuente="cli")


async def test_un_no_master_recibe_403_y_queda_registrado(monkeypatch) -> None:
    from core.admin import kill_switch_service as svc
    from core.api.errors import ApiError
    from core.api.routers import kill_switch as ruta

    denegados: list[str] = []

    async def _denegado(*, admin_id, etiqueta, fuente="api"):
        denegados.append(etiqueta)

    monkeypatch.setattr(svc, "registrar_denegado", _denegado)
    monkeypatch.setattr(ruta, "es_master", lambda _admin: False)
    principal = SimpleNamespace(admin=SimpleNamespace(id=uuid.uuid4(), email="dueno@negocio.co"))
    with pytest.raises(ApiError) as exc:
        await ruta.fijar(ruta.KillSwitchIn(active=True, reason="prueba"), principal)
    assert exc.value.status_code == 403
    assert denegados == ["dueno@negocio.co"]
