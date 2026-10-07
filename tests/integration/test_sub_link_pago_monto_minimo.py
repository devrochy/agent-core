"""HU-ARCH-18 / defecto 12: `sub_link_pago` con monto bajo el mínimo devuelve
`monto_bajo_minimo` (resultado controlado) y NO propaga una excepción que
apagaría el pack.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

TENANT = uuid.UUID("10000000-0000-0000-0000-000000000009")


class _CapRechaza:
    plugin_id = "pack_payments_fake"

    def methods(self):
        return []

    async def create_link(self, **_kwargs):
        from core.payments import PaymentCapabilityError

        raise PaymentCapabilityError("monto menor al mínimo del proveedor")


async def test_sub_link_pago_monto_bajo_minimo(monkeypatch):
    from pack_subscriptions.capability import SubscriptionCapabilityImpl
    from pack_subscriptions.models import Subscription, SubscriptionCharge

    import core.agents.spec as spec_mod
    import core.payments as payments_mod
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.subscriptions import SubscriptionError

    customer_id = uuid.uuid4()

    async def _noop_spec(*_a, **_k):
        return None

    monkeypatch.setattr(spec_mod, "load_active_spec", _noop_spec)
    monkeypatch.setattr(spec_mod, "enabled_packs", lambda spec: {"pack_payments_fake"})
    monkeypatch.setattr(payments_mod.payment_registry, "_caps", {"pack_payments_fake": _CapRechaza()})

    async with tenant_scope(TENANT):
        async with get_session() as session:
            sub = Subscription(
                tenant_id=TENANT, customer_id=customer_id, concepto="Plan",
                valor_periodo=54_000, estado="activa",
            )
            session.add(sub)
            await session.flush()
            session.add(
                SubscriptionCharge(
                    tenant_id=TENANT, subscription_id=sub.id,
                    periodo=datetime.now(UTC).strftime("%Y-%m"),
                    monto=54_000, estado="pendiente",
                )
            )
            sub_id = sub.id

        with pytest.raises(SubscriptionError) as exc:
            await SubscriptionCapabilityImpl().charge_link(sub_id)

    assert exc.value.code == "monto_bajo_minimo"
