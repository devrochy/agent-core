"""Capacidad de pagos de pack-payments (ADR-0067).

Expone el proveedor Wompi como `PaymentCapability` del core para que **otros
packs** (p. ej. cobranza) creen links de pago sin importar este pack. Los
métodos que ofrece Wompi (tarjeta, PSE, Nequi, Bancolombia) los elige el pagador
en el checkout, así que la capacidad publica **un** método "pago en línea / QR".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from core.db.session import get_session
from core.payments import (
    EN_REVISION,
    MONTO_BAJO_MINIMO,
    MONTO_NO_COINCIDE,
    NO_DISPONIBLE,
    SIN_PASARELA,
    YA_PAGADO,
    PaymentCapabilityError,
    PaymentLink,
    PaymentMethod,
)
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_payments.models import PaymentTransaction, utcnow
from pack_payments.proveedor import build_provider, tenant_config
from pack_payments.providers.base import MontoBajoMinimo, PaymentLinkResult, PaymentRequestRejected
from pack_payments.providers.wompi import min_amount_cop

log = get_logger(__name__)

PLUGIN_ID = "pack_payments"

# Estado de una transacción cuyo pago llegó por otro monto (lo pone el webhook,
# `module.MONTO_DISTINTO`; se repite para no importar el módulo).
MONTO_DISTINTO = "monto_distinto"

# Un link que vence en minutos no sirve para mandárselo al cliente: se trata como vencido.
MARGEN_VENCIMIENTO = timedelta(minutes=15)


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

    async def monto_minimo(self) -> float | None:
        """Mínimo por link del ambiente del negocio (sandbox o producción), para quien decide
        antes de intentar el cobro (`core.payments.monto_minimo`). `None` si no hay config."""
        try:
            config = await tenant_config()
        except Exception:
            return None
        return float(min_amount_cop(config.sandbox))

    async def create_link(
        self, *, reference: str, amount: float, description: str = "", method: str | None = None
    ) -> PaymentLink:
        """Idempotente por `reference`: si ya hay un link vigente con ese monto, se
        devuelve ese (antes se creaba otro en Wompi y el insert chocaba con el índice
        único: link huérfano y una excepción que apagaba el pack; journeys HU-SUB-12).

        Un link vencido se reemplaza por uno nuevo, también si era por otro monto (p. ej.
        el saldo de una cuota después de un abono): la transacción vieja queda borrada
        para auditoría y para el webhook tardío. Los rechazos llevan `code` (adenda
        2026-10-10 de ADR-0067) para que el consumidor no los confunda.
        """
        transacciones = await _transacciones(reference)
        _rechazar_si_no_cobrable(reference, transacciones)
        try:
            config = await tenant_config()
        except Exception as exc:  # config ausente/inválida del tenant
            raise PaymentCapabilityError(
                "La pasarela de pagos no está configurada.", code=SIN_PASARELA
            ) from exc
        viva = _viva(transacciones)
        vigente = _vigente(reference, amount, viva, config.link_expira_horas)
        if vigente is not None:
            return _link(vigente)

        provider = build_provider(config)
        try:
            nuevo = await provider.create_payment_link(
                reference=reference,
                amount=amount,
                currency=config.currency,
                expires_in_hours=config.link_expira_horas,
            )
        except MontoBajoMinimo as exc:
            raise PaymentCapabilityError(str(exc), code=MONTO_BAJO_MINIMO) from exc
        except PaymentRequestRejected as exc:
            # También cubre 401 (credenciales vencidas) y 5xx: si queda en warning, el
            # negocio pierde los pagos en línea sin que nadie se entere (ADR-0088).
            log.error("wompi_payment_link_rechazado", reference=reference, reason=str(exc))
            raise PaymentCapabilityError(
                "La pasarela no pudo generar el link de pago.", code=NO_DISPONIBLE
            ) from exc

        guardada = await _guardar(
            reference, amount, config.currency, provider.provider_name, nuevo, reemplaza=viva
        )
        if guardada is not None:
            if viva is not None:
                log.info(
                    "wompi_payment_link_regenerado",
                    reference=reference,
                    provider_reference_anterior=viva.provider_reference,
                )
            return _link(guardada)

        # Otra llamada reemplazó o creó la transacción viva mientras se pedía el link, o
        # un webhook le cambió el estado: el link recién creado en Wompi queda sin usar
        # (vence solo) y se decide de nuevo con lo que hay ahora.
        log.error(
            "wompi_payment_link_huerfano",
            reference=reference,
            provider_reference_sin_usar=nuevo.provider_reference,
        )
        actuales = await _transacciones(reference)
        _rechazar_si_no_cobrable(reference, actuales)
        vigente = _vigente(reference, amount, _viva(actuales), config.link_expira_horas)
        if vigente is not None:
            return _link(vigente)
        raise PaymentCapabilityError(
            "No se pudo generar el link de pago ahora. Intentá de nuevo en un momento.",
            code=NO_DISPONIBLE,
        )

    async def procesar_evento(self, payload: dict[str, Any]) -> str:
        """Operación opcional (HU-SH-04, ADR-0123, riesgo R-05): un evento de Wompi que llegó
        por la URL de la plataforma porque la cuenta es compartida. Se verifica con el
        secreto de eventos de ESTE negocio y se aplica igual que en el webhook propio."""
        from pack_payments.module import procesar_evento_de_wompi

        return await procesar_evento_de_wompi(payload)


def link_vencido(
    transaction: PaymentTransaction, horas_vigencia: int, ahora: datetime | None = None
) -> bool:
    """El link de la transacción ya no sirve para cobrar (o está por vencer).

    La tabla no guarda el vencimiento: se calcula con `created_at` y la vigencia
    configurada HOY (`link_expira_horas`). Si el negocio la cambió después de crear el
    link, el cálculo se corre en esa diferencia.
    """
    if transaction.status == "expirado" or not transaction.payment_link_url:
        return True
    ahora = ahora or utcnow()
    vence = transaction.created_at + timedelta(hours=horas_vigencia)
    return ahora >= vence - MARGEN_VENCIMIENTO


def _mismo_monto(a: Any, b: Any) -> bool:
    """Misma tolerancia que el webhook (`module._monto_coincide`)."""
    return abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal("0.01")


def _rechazar_si_no_cobrable(reference: str, transacciones: list[PaymentTransaction]) -> None:
    """La referencia no admite otro cobro, viva o ya reemplazada la transacción."""
    if any(t.status == "aprobado" for t in transacciones):
        raise PaymentCapabilityError(f"La referencia {reference} ya está pagada.", code=YA_PAGADO)
    # También en una transacción ya reemplazada: un webhook tardío del link viejo puede
    # haber dejado plata en revisión.
    if any(t.status == MONTO_DISTINTO for t in transacciones):
        raise PaymentCapabilityError(
            f"La referencia {reference} tiene un pago en revisión manual (llegó por otro monto).",
            code=EN_REVISION,
        )


def _viva(transacciones: list[PaymentTransaction]) -> PaymentTransaction | None:
    return next((t for t in transacciones if t.deleted_at is None), None)


def _vigente(
    reference: str, amount: float, viva: PaymentTransaction | None, horas_vigencia: int
) -> PaymentTransaction | None:
    """La viva si su link todavía sirve; `None` si hay que crear uno.

    El monto solo se compara contra un link vigente: uno vencido por otro monto se
    reemplaza (si no, la referencia quedaría bloqueada para siempre).
    """
    if viva is None or link_vencido(viva, horas_vigencia):
        return None
    if not _mismo_monto(viva.amount, amount):
        raise PaymentCapabilityError(
            f"Ya existe un cobro vigente con la referencia {reference} por otro monto.",
            code=MONTO_NO_COINCIDE,
        )
    return viva


def _link(transaction: PaymentTransaction) -> PaymentLink:
    return PaymentLink(
        reference=transaction.reference,
        url=transaction.payment_link_url,
        qr_payload=transaction.payment_link_url,
        method="wompi",
    )


async def _transacciones(reference: str) -> list[PaymentTransaction]:
    """Todas las de la referencia, también las borradas (reemplazadas por vencimiento)."""
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(PaymentTransaction).where(
                        PaymentTransaction.tenant_id == get_current_tenant_id(),
                        PaymentTransaction.reference == reference,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _guardar(
    reference: str,
    amount: float,
    currency: str,
    provider_name: str,
    nuevo: PaymentLinkResult,
    *,
    reemplaza: PaymentTransaction | None,
) -> PaymentTransaction | None:
    """Guarda la transacción del link nuevo; `None` si la viva cambió desde que se leyó.

    La del link vencido queda borrada (auditoría y webhook tardío por
    `provider_reference`), nunca dos vivas por referencia (índice único parcial de la
    migración pack_payments_0003). El `where` exige que siga como se leyó.
    """
    try:
        async with get_session() as session:
            if reemplaza is not None:
                borrada = await session.execute(
                    update(PaymentTransaction)
                    .where(
                        PaymentTransaction.tenant_id == get_current_tenant_id(),
                        PaymentTransaction.id == reemplaza.id,
                        PaymentTransaction.deleted_at.is_(None),
                        PaymentTransaction.status == reemplaza.status,
                    )
                    .values(
                        deleted_at=utcnow(),
                        status="expirado" if reemplaza.status == "pendiente" else reemplaza.status,
                    )
                )
                if borrada.rowcount == 0:
                    return None
            transaction = PaymentTransaction(
                reference=reference,
                provider=provider_name,
                provider_reference=nuevo.provider_reference,
                status="pendiente",
                amount=amount,
                currency=currency,
                payment_link_url=nuevo.payment_link_url,
                # La capacidad no tiene ToolContext (la consume otro pack):
                # idempotencia por reference la garantiza el índice único, este
                # key solo debe satisfacer el NOT NULL (ADR-0067).
                idempotency_key=str(uuid.uuid4()),
            )
            session.add(transaction)
            # Dos llamadas sin viva a la vez: la segunda choca con el índice único
            # parcial acá y no como una excepción que apague el pack.
            await session.flush()
            return transaction
    except IntegrityError:
        return None
