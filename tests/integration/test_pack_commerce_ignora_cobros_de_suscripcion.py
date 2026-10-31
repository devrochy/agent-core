"""SPEC-43 · HU-COM-17: `payment.confirmed` con una referencia de suscripción no es de commerce.

El cobro de una suscripción (`sub-<cargo>`) o de la plataforma (`sub-<tenant>-…`) lo concilia `pack_subscriptions`:
commerce lo ignora sin registrar `payment_event_no_matching_order`. Una referencia de pedido sin pedido sí se alarma.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from structlog.testing import capture_logs

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def test_una_referencia_sub_se_ignora_sin_alarma_y_una_de_pedido_sin_pedido_si_alarma() -> None:
    from pack_commerce.module import CRMModule

    from core.alta_negocio import onboard_tenant
    from core.shared.tenant import tenant_scope

    tid, _ = await onboard_tenant(name="Comercio", slug=uniq("subref"), packs=["pack_commerce"], sembrar_ejemplos=False)
    modulo = CRMModule()
    async with tenant_scope(tid):
        with capture_logs() as logs:
            await modulo._update_order_payment_status(f"sub-{uuid.uuid4()}", "pagado", 100000.0)
            await modulo._update_order_payment_status(f"sub-{tid}-basic-202610", "pagado", 100000.0)
        assert [e for e in logs if e["event"] == "payment_event_no_matching_order"] == []

        with capture_logs() as logs:
            await modulo._update_order_payment_status("OC-9999", "pagado", 100000.0)
        assert [e["reference"] for e in logs if e["event"] == "payment_event_no_matching_order"] == ["OC-9999"]
