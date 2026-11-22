"""SPEC-43 · HU-COM-17: `payment.confirmed` con una referencia de suscripción no es de commerce.

El cobro de una suscripción (`sub-<cargo>`) o de la plataforma (`sub-<tenant>-…`) lo concilia `pack_subscriptions`:
commerce lo ignora sin registrar `payment_event_no_matching_order`. Una referencia de pedido sin pedido sí se alarma.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


class _LogQueAnota:
    """Sustituto del logger del módulo: con `cache_logger_on_first_use` (lo activa `configure_logging` al importar
    `core.main`) el logger queda atado a la lista de procesadores de su primer uso, y si otro test configura una lista
    nueva `capture_logs` deja de ver sus eventos (pasó en `test_tool_sin_binding_v3.py`, core#850)."""

    def __init__(self, eventos: list[dict[str, Any]]) -> None:
        self._eventos = eventos

    def __getattr__(self, nivel: str) -> Any:
        def anotar(event: str, **kw: Any) -> None:
            self._eventos.append({"event": event, "log_level": nivel, **kw})

        return anotar


async def test_una_referencia_sub_se_ignora_sin_alarma_y_una_de_pedido_sin_pedido_si_alarma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pack_commerce.module as modulo_commerce
    from _habilidades import onboard_tenant
    from pack_commerce.module import CRMModule

    from core.shared.tenant import tenant_scope

    tid, _ = await onboard_tenant(name="Comercio", slug=uniq("subref"), packs=["pack_commerce"], sembrar_ejemplos=False)
    modulo = CRMModule()
    logs: list[dict[str, Any]] = []
    monkeypatch.setattr(modulo_commerce, "log", _LogQueAnota(logs))
    async with tenant_scope(tid):
        await modulo._update_order_payment_status(f"sub-{uuid.uuid4()}", "pagado", 100000.0)
        await modulo._update_order_payment_status(f"sub-{tid}-basic-202610", "pagado", 100000.0)
        assert [e for e in logs if e["event"] == "payment_event_no_matching_order"] == []

        logs.clear()
        await modulo._update_order_payment_status("OC-9999", "pagado", 100000.0)
        assert [e["reference"] for e in logs if e["event"] == "payment_event_no_matching_order"] == ["OC-9999"]
