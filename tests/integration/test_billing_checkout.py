"""Checkout y webhook de la suscripción de plataforma (ADR-0046).

Wompi se mockea (sin red): se prueba la persistencia del pago, la idempotencia
del webhook, la activación/extensión de la suscripción y el aislamiento (RLS).
Genéricos: tenants desechables por test.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


async def _onboard(slug: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant checkout {slug}",
        slug=slug,
        business_description="Checkout de plan (test genérico).",
    )
    return tid


# Monto firmado del webhook: el del link (ADR-0123 lo compara con el del pago).
_CENTAVOS_POR_PLAN = {"basic": 20_000_000, "premium": 60_000_000}


class _FakeProvider:
    """Proveedor con la firma/parseo REALES de Wompi y la red mockeada."""

    provider_name = "wompi"
    events_secret = "wh-secret"

    def __init__(self):
        self.created: list[dict] = []
        from core.billing.providers.wompi import WompiBillingProvider

        self._real = WompiBillingProvider(
            public_key="pub", private_key="prv", events_secret="wh-secret", sandbox=True
        )

    async def create_payment_link(self, *, reference, amount_cop, expires_in_hours=24, plan_name=""):
        from core.billing.providers.base import PaymentLink

        self.created.append({"reference": reference, "amount_cop": amount_cop})
        return PaymentLink(provider_reference=f"link-{reference}", url=f"https://checkout.test/{reference}")

    def verify_webhook_signature(self, payload):
        return self._real.verify_webhook_signature(payload)

    def parse_webhook_event(self, payload):
        return self._real.parse_webhook_event(payload)


async def test_start_subscription_crea_pago_y_link(monkeypatch):
    from core.billing import checkout
    from core.db.models import SubscriptionPayment
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    fake = _FakeProvider()
    monkeypatch.setattr(checkout, "get_billing_provider", lambda: fake)

    tid = await _onboard(_uniq("chk"))
    async with tenant_scope(tid):
        url = await checkout.start_subscription("basic")
        async with get_session() as session:
            payment = (
                await session.execute(
                    select(SubscriptionPayment).where(SubscriptionPayment.tenant_id == tid)
                )
            ).scalar_one()
    assert url and url.startswith("https://checkout.test/")
    assert payment.status == "pendiente"
    assert float(payment.amount_cop) == 200_000
    assert payment.checkout_url == url
    assert fake.created and fake.created[0]["amount_cop"] == 200_000


async def test_apply_payment_aprobado_activa(monkeypatch):
    from core.billing import checkout
    from core.billing.service import get_active_subscription
    from core.db.models import SubscriptionPayment
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    fake = _FakeProvider()
    monkeypatch.setattr(checkout, "get_billing_provider", lambda: fake)

    tid = await _onboard(_uniq("act"))
    async with tenant_scope(tid):
        await checkout.start_subscription("premium")
        async with get_session() as session:
            ref = (
                await session.execute(
                    select(SubscriptionPayment.provider_reference).where(
                        SubscriptionPayment.tenant_id == tid
                    )
                )
            ).scalar_one()

    updated = await checkout.apply_payment(provider_reference=ref, status="aprobado", amount_in_cents=_CENTAVOS_POR_PLAN["premium"])
    assert updated is True
    sub = await get_active_subscription(tid)
    assert sub.plan_id == "premium"
    assert sub.status == "active"
    assert sub.period_end is not None
    assert sub.trial_ends_at is None


async def test_apply_payment_idempotente(monkeypatch):
    from core.billing import checkout
    from core.db.models import SubscriptionPayment
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    fake = _FakeProvider()
    monkeypatch.setattr(checkout, "get_billing_provider", lambda: fake)

    tid = await _onboard(_uniq("idem"))
    async with tenant_scope(tid):
        await checkout.start_subscription("basic")
        async with get_session() as session:
            ref = (
                await session.execute(
                    select(SubscriptionPayment.provider_reference).where(
                        SubscriptionPayment.tenant_id == tid
                    )
                )
            ).scalar_one()

    assert await checkout.apply_payment(provider_reference=ref, status="aprobado", amount_in_cents=_CENTAVOS_POR_PLAN["basic"]) is True
    # Segundo intento del webhook con el MISMO evento: no reaplica.
    assert await checkout.apply_payment(provider_reference=ref, status="aprobado", amount_in_cents=_CENTAVOS_POR_PLAN["basic"]) is False
    async with tenant_scope(tid):
        async with get_session() as session:
            payment = (
                await session.execute(
                    select(SubscriptionPayment).where(SubscriptionPayment.tenant_id == tid)
                )
            ).scalar_one()
    assert payment.status == "aprobado"


async def test_apply_payment_rechazado_no_activa(monkeypatch):
    from core.billing import checkout
    from core.billing.service import get_active_subscription
    from core.db.models import SubscriptionPayment
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    fake = _FakeProvider()
    monkeypatch.setattr(checkout, "get_billing_provider", lambda: fake)

    tid = await _onboard(_uniq("rech"))
    async with tenant_scope(tid):
        await checkout.start_subscription("basic")
        async with get_session() as session:
            ref = (
                await session.execute(
                    select(SubscriptionPayment.provider_reference).where(
                        SubscriptionPayment.tenant_id == tid
                    )
                )
            ).scalar_one()

    await checkout.apply_payment(provider_reference=ref, status="rechazado", amount_in_cents=None)
    sub = await get_active_subscription(tid)
    assert sub.status == "trialing"  # sigue en free/trial, no se activó


async def test_payment_aislado_entre_tenants(monkeypatch):
    from core.billing import checkout
    from core.db.models import SubscriptionPayment
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    fake = _FakeProvider()
    monkeypatch.setattr(checkout, "get_billing_provider", lambda: fake)

    a = await _onboard(_uniq("pa"))
    b = await _onboard(_uniq("pb"))
    async with tenant_scope(a):
        await checkout.start_subscription("basic")
    async with tenant_scope(a):
        async with get_session() as session:
            rows = (await session.execute(select(SubscriptionPayment.tenant_id))).scalars().all()
    assert rows and all(t == a for t in rows)

    async with tenant_scope(b):
        async with get_session() as session:
            rows = (await session.execute(select(SubscriptionPayment.tenant_id))).scalars().all()
    assert rows == []


def _signed_wompi_payload(
    payment_link_id: str, status: str, secret: str, amount_in_cents: int = 20_000_000
) -> dict:
    """Como Wompi: la firma cubre el estado y el monto (ADR-0123 exige el monto firmado)."""
    timestamp = 1700000000
    data = {
        "transaction": {"payment_link_id": payment_link_id, "status": status, "amount_in_cents": amount_in_cents}
    }
    checksum = hashlib.sha256(
        (str(status) + str(amount_in_cents) + str(timestamp) + secret).encode()
    ).hexdigest()
    return {
        "signature": {"properties": ["transaction.status", "transaction.amount_in_cents"], "checksum": checksum},
        "timestamp": timestamp,
        "data": data,
    }


async def test_webhook_billing_activa_suscripcion(app_client, monkeypatch):
    from core.billing import checkout
    from core.billing.service import get_active_subscription
    from core.config import get_settings
    from core.db.models import SubscriptionPayment
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    fake = _FakeProvider()
    monkeypatch.setattr(checkout, "get_billing_provider", lambda: fake)
    # El webhook resuelve el proveedor desde config: se parchea el factory del módulo.
    monkeypatch.setattr("core.billing.providers.get_billing_provider", lambda: fake)
    settings = get_settings()
    monkeypatch.setattr(settings, "wompi_billing_events_secret", "wh-secret", raising=False)
    monkeypatch.setattr(settings, "billing_provider", "wompi", raising=False)

    tid = await _onboard(_uniq("wh"))
    async with tenant_scope(tid):
        await checkout.start_subscription("basic")
        async with get_session() as session:
            ref = (
                await session.execute(
                    select(SubscriptionPayment.provider_reference).where(
                        SubscriptionPayment.tenant_id == tid
                    )
                )
            ).scalar_one()

    payload = _signed_wompi_payload(ref, "APPROVED", "wh-secret")
    resp = await app_client.post("/webhooks/billing/wompi", json=payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    sub = await get_active_subscription(tid)
    assert sub.status == "active"
    assert sub.plan_id == "basic"


async def test_webhook_billing_firma_invalida(app_client, monkeypatch):
    from core.billing import checkout
    from core.billing.service import get_active_subscription
    from core.config import get_settings
    from core.db.models import SubscriptionPayment
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    fake = _FakeProvider()
    monkeypatch.setattr(checkout, "get_billing_provider", lambda: fake)
    monkeypatch.setattr("core.billing.providers.get_billing_provider", lambda: fake)
    settings = get_settings()
    monkeypatch.setattr(settings, "wompi_billing_events_secret", "wh-secret", raising=False)
    monkeypatch.setattr(settings, "billing_provider", "wompi", raising=False)

    tid = await _onboard(_uniq("whbad"))
    async with tenant_scope(tid):
        await checkout.start_subscription("basic")
        async with get_session() as session:
            ref = (
                await session.execute(
                    select(SubscriptionPayment.provider_reference).where(
                        SubscriptionPayment.tenant_id == tid
                    )
                )
            ).scalar_one()

    # Firmado con OTRO secreto: se rechaza con 401 y NO se activa.
    payload = _signed_wompi_payload(ref, "APPROVED", "secreto-malo")
    resp = await app_client.post("/webhooks/billing/wompi", json=payload)
    assert resp.status_code == 401
    sub = await get_active_subscription(tid)
    assert sub.status == "trialing"


async def test_apply_payment_sin_monto_firmado_no_activa(monkeypatch):
    """HU-SH-04: sin el monto firmado por Wompi (o con otro), el pago no se aplica."""
    from core.billing import checkout
    from core.billing.service import get_active_subscription
    from core.db.models import SubscriptionPayment
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    fake = _FakeProvider()
    monkeypatch.setattr(checkout, "get_billing_provider", lambda: fake)
    monkeypatch.setattr("core.shared.alerts.send_alert", _sin_alerta)

    tid = await _onboard(_uniq("firma"))
    async with tenant_scope(tid):
        await checkout.start_subscription("basic")
        async with get_session() as session:
            ref = (
                await session.execute(
                    select(SubscriptionPayment.provider_reference).where(SubscriptionPayment.tenant_id == tid)
                )
            ).scalar_one()
    assert await checkout.apply_payment(provider_reference=ref, status="aprobado", amount_in_cents=None) is False
    sub = await get_active_subscription(tid)
    assert sub.plan_id == "free"
    async with tenant_scope(tid):
        async with get_session() as session:
            pago = (
                await session.execute(select(SubscriptionPayment).where(SubscriptionPayment.tenant_id == tid))
            ).scalar_one()
    assert pago.status == "monto_distinto"


async def _sin_alerta(*_args, **_kwargs) -> None:
    return None
