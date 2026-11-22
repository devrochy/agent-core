"""Los crons de Captar leads (ADR-0193 §3) saltan el negocio sin config y un negocio que falla no corta a los demás.

Antes vivían en `pack_commerce.jobs` (incidente del entorno de test 2026-10-11: un negocio sin config cortaba el
recorrido). En el core la config es la fila de `lead_settings`: sin fila (o ilegible) `cargar_del_negocio` da None y el
negocio se salta en silencio, la misma conducta que tenía un negocio sin commerce.
"""

from __future__ import annotations

import uuid

import pytest

from core.leads import jobs
from core.leads.config import ConfigCaptarLeads
from core.shared.tenant import get_current_tenant_id

SIN_CONFIG = uuid.UUID("20000000-0000-0000-0000-000000000001")
FALLA = uuid.UUID("20000000-0000-0000-0000-000000000003")
CON_CONFIG = uuid.UUID("20000000-0000-0000-0000-000000000002")


class _LogQueAnota:
    """Sustituto del logger del módulo (structlog cachea el logger: `capture_logs` no ve los eventos de un logger ya
    usado)."""

    def __init__(self, eventos: list[dict]) -> None:
        self._eventos = eventos

    def __getattr__(self, nivel: str):
        def anotar(event: str, **kw) -> None:
            self._eventos.append({"event": event, "log_level": nivel, **kw})

        return anotar


@pytest.mark.parametrize(
    ("cron", "por_negocio", "cron_name"),
    [
        (jobs.follow_up_leads, "_follow_up_leads_del_negocio", jobs.CRON_LEADS),
        (jobs.recordar_seguimientos, "_recordar_del_negocio", jobs.CRON_SEGUIMIENTOS),
    ],
)
async def test_salta_el_negocio_sin_config_y_el_que_falla_no_corta_a_los_demas(
    monkeypatch: pytest.MonkeyPatch, cron, por_negocio: str, cron_name: str
) -> None:
    from core.leads import recordatorios

    eventos: list[dict] = []
    procesados: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    fallos: list[tuple[uuid.UUID, str, str]] = []
    config = ConfigCaptarLeads()

    async def negocios() -> list[uuid.UUID]:
        # El sin config y el que falla van primero: no pueden cortar el recorrido.
        return [SIN_CONFIG, FALLA, CON_CONFIG]

    async def cargar(tenant_id: uuid.UUID):
        assert tenant_id == get_current_tenant_id()
        return None if tenant_id == SIN_CONFIG else config

    async def del_negocio(tenant_id: uuid.UUID, cfg):
        assert cfg is config
        if tenant_id == FALLA:
            raise RuntimeError("bd")
        procesados.append((tenant_id, get_current_tenant_id()))
        if por_negocio == "_recordar_del_negocio":
            return recordatorios.Conteo(), False
        return 3, 2

    async def si(*_a, **_k) -> bool:
        return True

    async def no(*_a, **_k) -> bool:
        return False

    async def registrar(*_a, **_k) -> None:
        return None

    async def registrar_fallo(tenant_id: uuid.UUID, nombre: str, *, error_class: str) -> None:
        fallos.append((tenant_id, nombre, error_class))

    monkeypatch.setattr(jobs, "_negocios_con_captar_leads", negocios)
    monkeypatch.setattr(jobs.config_leads, "cargar_del_negocio", cargar)
    monkeypatch.setattr(jobs, por_negocio, del_negocio)
    monkeypatch.setattr(jobs, "debe_correr", si)

    async def sin_freno(*_a, **_k):
        return None  # el plan incluye el seguimiento (HU-BILL-26)

    monkeypatch.setattr(jobs, "plan_que_frena", sin_freno)
    monkeypatch.setattr(jobs.pausa_automatica, "negocio_pausado", no)
    monkeypatch.setattr(jobs, "registrar", registrar)
    monkeypatch.setattr(jobs, "registrar_fallo", registrar_fallo)
    monkeypatch.setattr(jobs, "log", _LogQueAnota(eventos))

    await cron()

    assert procesados == [(CON_CONFIG, CON_CONFIG)]
    assert fallos == [(FALLA, cron_name, "RuntimeError")]
    if cron is jobs.follow_up_leads:
        assert {"event": "follow_up_completed", "log_level": "info", "kind": "lead", "candidates": 3, "sent": 2} in eventos
    assert [e["event"] for e in eventos if e["log_level"] == "exception"] == [
        "follow_up_leads_fallo" if cron is jobs.follow_up_leads else "recordar_seguimientos_fallo"
    ]
