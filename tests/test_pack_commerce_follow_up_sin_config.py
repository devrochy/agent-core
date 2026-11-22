"""El cron de seguimiento de cotizaciones de pack-commerce salta el negocio sin configuración.

Visto en el entorno de test (2026-10-11): un negocio con el pack habilitado pero sin
configurar hacía que `load_config` lanzara `PluginConfigError` dentro del recorrido;
el cron terminaba en `cron_task_failed` y los negocios siguientes no se procesaban. El de leads es del core desde
ADR-0193: su caso está en `tests/test_leads_follow_up_sin_config.py`.
"""

from __future__ import annotations

import uuid

import pytest
from pack_commerce import jobs
from pack_commerce.config import CRMConfig

from core.plugin_manager import plugin_manager
from core.shared.exceptions import PluginConfigError
from core.shared.tenant import get_current_tenant_id

SIN_CONFIG = uuid.UUID("20000000-0000-0000-0000-000000000001")
CON_CONFIG = uuid.UUID("20000000-0000-0000-0000-000000000002")


class _LogQueAnota:
    """Sustituto del logger del módulo (structlog cachea el logger: `capture_logs` no ve
    los eventos de un logger ya usado)."""

    def __init__(self, eventos: list[dict]) -> None:
        self._eventos = eventos

    def __getattr__(self, nivel: str):
        def anotar(event: str, **kw) -> None:
            self._eventos.append({"event": event, "log_level": nivel, **kw})

        return anotar


@pytest.mark.parametrize(
    ("cron", "por_negocio", "kind"),
    [
        (jobs.follow_up_quotes, "_follow_up_quotes_del_negocio", "quote"),
    ],
)
async def test_salta_el_negocio_sin_config_y_sigue_con_los_demas(
    monkeypatch: pytest.MonkeyPatch, cron, por_negocio: str, kind: str
) -> None:
    eventos: list[dict] = []
    procesados: list[tuple[uuid.UUID, uuid.UUID | None]] = []
    config = CRMConfig()

    async def tenant_ids() -> list[uuid.UUID]:
        # El negocio sin config va primero: antes cortaba el recorrido.
        return [SIN_CONFIG, CON_CONFIG]

    async def load_config(plugin_id: str):
        assert plugin_id == jobs.PACK_ID
        if get_current_tenant_id() == SIN_CONFIG:
            raise PluginConfigError(plugin_id, "El plugin requiere configuración previa")
        return config

    async def seguimiento(tenant_id: uuid.UUID, cfg) -> tuple[int, int]:
        assert cfg is config
        procesados.append((tenant_id, get_current_tenant_id()))
        return 3, 2

    async def debe_correr(tenant_id: uuid.UUID, cron_name: str) -> bool:
        # ADR-0201: la política por negocio se prueba aparte; aquí solo importa el recorrido.
        return True

    async def registrar(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(jobs, "_tenant_ids", tenant_ids)
    monkeypatch.setattr(plugin_manager, "load_config", load_config)
    monkeypatch.setattr(jobs, por_negocio, seguimiento)
    monkeypatch.setattr(jobs, "debe_correr", debe_correr)
    monkeypatch.setattr(jobs, "registrar", registrar)

    async def sin_freno(*_a, **_k):
        return None  # el plan incluye el seguimiento de cotizaciones (HU-BILL-26)

    if hasattr(jobs, "plan_que_frena"):
        monkeypatch.setattr(jobs, "plan_que_frena", sin_freno)
    monkeypatch.setattr(jobs, "log", _LogQueAnota(eventos))

    await cron()  # no lanza: el wrapper de `register_cron` no registra `cron_task_failed`

    assert procesados == [(CON_CONFIG, CON_CONFIG)]
    assert {"event": "follow_up_sin_config", "log_level": "warning", "kind": kind, "tenant_id": str(SIN_CONFIG)} in eventos
    assert {"event": "follow_up_completed", "log_level": "info", "kind": kind, "candidates": 3, "sent": 2} in eventos
    assert not [e for e in eventos if e["log_level"] in ("error", "exception")]
