"""El link de un cargo no se pide dos veces a la pasarela (encontrado por los journeys de HU-SUB-12).

El cron genera el cargo con su link; cuando el cliente pedía el link (`sub_link_pago`), se
creaba otro en la pasarela con la misma referencia y el insert chocaba con el índice único.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


class _Proveedor:
    provider_name = "simulado"

    def __init__(self) -> None:
        self.referencias: list[str] = []  # el cron recorre todos los negocios: se cuenta por referencia

    async def create_payment_link(self, *, reference, amount, currency, expires_in_hours):  # type: ignore[no-untyped-def]
        from pack_payments.providers.base import PaymentLinkResult

        self.referencias.append(reference)
        return PaymentLinkResult(provider_reference=f"sim-{reference}", payment_link_url=f"https://pagos.simulados/{reference}")


@pytest.fixture()
def proveedor(monkeypatch):
    import pack_payments.payments as capacidad

    falso = _Proveedor()
    monkeypatch.setattr(capacidad, "build_provider", lambda _config: falso)
    return falso


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    tid, _ = await onboard_tenant(
        name="Negocio", slug=uniq("link"), packs=["pack_subscriptions"], plan="premium"
    )
    async with tenant_scope(tid):
        await plugin_manager.save_config(
            "pack_payments",
            {"public_key": "pub_test_x", "private_key": "prv_test_x", "events_secret": "test_x", "sandbox": True},
        )
        from core.admin.views import set_pack_for_tenant

        assert await set_pack_for_tenant("pack_payments", True) is None
    return tid


async def test_create_link_es_idempotente_por_referencia(proveedor) -> None:
    from pack_payments.models import PaymentTransaction
    from pack_payments.payments import WompiPaymentCapability

    from core.db.session import get_session
    from core.payments import PaymentCapabilityError
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cap = WompiPaymentCapability()
    async with tenant_scope(tid):
        primero = await cap.create_link(reference="sub-1", amount=200000)
        segundo = await cap.create_link(reference="sub-1", amount=200000)
        with pytest.raises(PaymentCapabilityError):
            await cap.create_link(reference="sub-1", amount=150000)
        async with get_session() as s:
            filas = (await s.execute(select(PaymentTransaction).where(PaymentTransaction.reference == "sub-1"))).scalars().all()
    assert primero.url == segundo.url
    assert proveedor.referencias.count("sub-1") == 1
    assert len(filas) == 1


async def test_sub_link_pago_reusa_el_link_que_genero_el_cron(proveedor) -> None:
    from pack_subscriptions.capability import SubscriptionCapabilityImpl, crear_suscripcion_para
    from pack_subscriptions.jobs import _generar_cargos
    from pack_subscriptions.models import SubscriptionCharge

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    plan = {"nombre": "Basic", "precio": 200000, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 0}
    async with tenant_scope(tid):
        sub = await crear_suscripcion_para(customer_id=uuid.uuid4(), variante_id=uuid.uuid4(), plan=plan)
    await _generar_cargos(datetime.now(UTC) + timedelta(days=29))
    async with tenant_scope(tid):
        async with get_session() as s:
            cargo = (
                await s.execute(select(SubscriptionCharge).where(SubscriptionCharge.subscription_id == sub.id))
            ).scalar_one()
        assert cargo.link_url
        link = await SubscriptionCapabilityImpl().charge_link(sub.id)
    assert link["url"] == cargo.link_url
    assert Decimal(str(link["monto"])) == Decimal("200000")
    assert proveedor.referencias.count(f"sub-{cargo.id}") == 1  # solo el del cron
