"""Capacidad de pagos de pack-payments (ADR-0067).

Expone el proveedor Wompi como `PaymentCapability` del core para que **otros
packs** (p. ej. cobranza) creen links de pago sin importar este pack. Los
métodos que ofrece Wompi (tarjeta, PSE, Nequi, Bancolombia) los elige el pagador
en el checkout, así que la capacidad publica **un** método "pago en línea / QR".
"""

from __future__ import annotations

import uuid

from core.db.session import get_session
from core.payments import PaymentCapabilityError, PaymentLink, PaymentMethod
from core.shared.logger import get_logger
from pack_payments.models import PaymentTransaction
from pack_payments.providers.base import PaymentRequestRejected
from pack_payments.skills import build_provider, tenant_config

log = get_logger(__name__)

PLUGIN_ID = "pack_payments"


class WompiPaymentCapability:
    plugin_id = PLUGIN_ID

    def methods(self) -> list[PaymentMethod]:
        return [
            PaymentMethod(
                id="wompi",
                label="Pago en línea / QR (tarjeta, PSE, Nequi, Bancolombia)",
                kind="qr",
            )
        ]

    async def create_link(
        self, *, reference: str, amount: float, description: str = "", method: str | None = None
    ) -> PaymentLink:
        """Idempotente por `reference`: si ya hay un link pendiente con ese monto, se
        devuelve ese (antes se creaba otro en Wompi y el insert chocaba con el índice
        único: link huérfano y una excepción que apagaba el pack; journeys HU-SUB-12)."""
        existente = await _transaccion(reference)
        if existente is not None:
            if existente.status == "pendiente" and float(existente.amount or 0) == float(amount) and existente.payment_link_url:
                return PaymentLink(
                    reference=reference,
                    url=existente.payment_link_url,
                    qr_payload=existente.payment_link_url,
                    method="wompi",
                )
            raise PaymentCapabilityError(
                f"Ya existe un cobro con la referencia {reference} ({existente.status}, {existente.amount})."
            )
        try:
            config = await tenant_config()
        except Exception as exc:  # config ausente/inválida del tenant
            raise PaymentCapabilityError("La pasarela de pagos no está configurada.") from exc
        provider = build_provider(config)
        try:
            link = await provider.create_payment_link(
                reference=reference,
                amount=amount,
                currency=config.currency,
                expires_in_hours=config.link_expira_horas,
            )
        except PaymentRequestRejected as exc:
            raise PaymentCapabilityError(str(exc)) from exc
        async with get_session() as session:
            session.add(
                PaymentTransaction(
                    reference=reference,
                    provider=provider.provider_name,
                    provider_reference=link.provider_reference,
                    status="pendiente",
                    amount=amount,
                    currency=config.currency,
                    payment_link_url=link.payment_link_url,
                    # La capacidad no tiene ToolContext (la consume otro pack):
                    # idempotencia por reference la garantiza el índice único, este
                    # key solo debe satisfacer el NOT NULL (ADR-0067).
                    idempotency_key=str(uuid.uuid4()),
                )
            )
        return PaymentLink(
            reference=reference,
            url=link.payment_link_url,
            qr_payload=link.payment_link_url,
            method="wompi",
        )


async def _transaccion(reference: str) -> PaymentTransaction | None:
    from sqlalchemy import select

    from core.shared.tenant import get_current_tenant_id

    async with get_session() as session:
        return (
            await session.execute(
                select(PaymentTransaction).where(
                    PaymentTransaction.tenant_id == get_current_tenant_id(),
                    PaymentTransaction.reference == reference,
                )
            )
        ).scalar_one_or_none()
