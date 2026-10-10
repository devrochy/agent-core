"""PaymentSkill: generación de links de pago en línea."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update

from core.db.session import get_session
from core.mcp.types import BaseSkill, MCPTool, ScopedHttp, ToolContext
from core.shared.exceptions import PluginError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_payments.config import PaymentsConfig
from pack_payments.models import PaymentTransaction, utcnow
from pack_payments.providers.base import PaymentRequestRejected
from pack_payments.providers.wompi import WompiProvider

log = get_logger(__name__)

PLUGIN_ID = "pack_payments"

# Estado de una transacción cuyo pago llegó por otro monto (lo pone el webhook,
# `module.MONTO_DISTINTO`; acá se repite para no importar el módulo).
MONTO_DISTINTO = "monto_distinto"


async def tenant_config() -> PaymentsConfig:
    """Config del pack para el tenant ACTUAL — nunca `self.config` (ver
    docs/PLUGIN-CONTRACT.md § 6): las credenciales de Wompi son por-tenant.
    """
    from core.plugin_manager import plugin_manager

    config = await plugin_manager.load_config(PLUGIN_ID)
    if config is None:
        raise PluginError(PLUGIN_ID, "Pasarela de pagos no configurada para este tenant")
    return config


def build_provider(config: PaymentsConfig, http: ScopedHttp | None = None) -> WompiProvider:
    """`http`: el `context.http` de la tool. Sin él (panel, capacidad de pagos), el
    proveedor usa `plugin_http` con la allowlist del pack (ADR-0052)."""
    return WompiProvider(
        public_key=config.public_key,
        private_key=config.private_key.get_secret_value(),
        events_secret=config.events_secret.get_secret_value(),
        sandbox=config.sandbox,
        http=http,
    )


class PaymentSkill(BaseSkill):
    name = "payment_skill"
    description = "Generación de links de pago en línea"
    keywords: list[str] = ["pagar", "pago en línea", "link de pago", "tarjeta", "PSE"]  # noqa: RUF012

    def get_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="generar_link_pago",
                description=(
                    "Genera un link de pago en línea (tarjeta/PSE/transferencia vía Wompi) para una "
                    "referencia de negocio ya existente (ej. el número de un pedido). Úsala SOLO cuando "
                    "el cliente eligió explícitamente pagar en línea, con el monto y la referencia "
                    "correctos (nunca inventes el monto: usá el que devolvió la tool que creó el "
                    "pedido/cotización). Wompi tiene un monto mínimo por cobro (más alto en el "
                    "ambiente de pruebas): si el total es menor, esta tool devuelve un error -- "
                    "ofrecé transferencia o contraentrega en vez de reintentar. Si devuelve un "
                    "error (referencia ya pagada, monto distinto al de la referencia), NO hay link: "
                    "no digas que lo generaste."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reference": {
                            "type": "string",
                            "description": "Referencia de negocio a cobrar (ej. el número público del pedido, 'OC-0001')",
                        },
                        "amount": {"type": "number", "description": "Monto a cobrar, en la moneda configurada del tenant"},
                    },
                    "required": ["reference", "amount"],
                },
                handler=self.generar_link_pago,
            ),
        ]

    async def generar_link_pago(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        reference = str(arguments["reference"]).strip()
        try:
            amount = float(arguments["amount"])
        except (TypeError, ValueError):
            return {"error": "amount inválido: debe ser un número."}
        if not reference:
            return {"error": "reference vacía."}
        if amount <= 0:
            return {"error": "amount debe ser mayor a 0."}

        try:
            config = await tenant_config()
        except PluginError as exc:
            # Config faltante para ESTE tenant -- no es una falla del plugin,
            # no debe disparar auto_disable (mismo criterio que
            # PaymentRequestRejected: un problema de datos, no de código).
            return {"error": str(exc)}
        provider = build_provider(config, http=context.http)

        # Idempotencia (invariante del contrato de plugin): si esta misma llamada de
        # tool ya corrió antes (reintento del loop), no generar un segundo link.
        async with get_session() as session:
            existing = (
                await session.execute(
                    select(PaymentTransaction).where(
                        PaymentTransaction.tenant_id == get_current_tenant_id(),
                        PaymentTransaction.idempotency_key == context.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _result(existing)

        # Idempotencia por REFERENCE (un pedido = una transacción): si el modelo
        # llama esta tool dos veces para el MISMO pedido en turnos distintos (no
        # el mismo tool_call -- eso ya lo cubre el chequeo de arriba), se reusa la
        # transacción existente en vez de reventar contra el índice único
        # (tenant_id, reference) -- mismo patrón de bug ya corregido en
        # pack_commerce (excepción no manejada -> PluginError -> auto_disable).
        # Reusarla a ciegas también falla (test, 2026-10-10: se devolvió el link
        # vencido de un pedido ya pagado y por otro monto, y el asistente lo
        # ofreció como nuevo): antes se mira el estado, el monto y el vencimiento.
        async with get_session() as session:
            de_la_referencia = (
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
        reemplaza: PaymentTransaction | None = None
        if de_la_referencia:
            rechazo = _referencia_no_cobrable(reference, amount, de_la_referencia)
            if rechazo is not None:
                return rechazo
            viva = next((t for t in de_la_referencia if t.deleted_at is None), None)
            if viva is not None:
                if not link_vencido(viva, config.link_expira_horas):
                    return _result(viva)
                reemplaza = viva

        try:
            link = await provider.create_payment_link(
                reference=reference,
                amount=amount,
                currency=config.currency,
                expires_in_hours=config.link_expira_horas,
            )
        except PaymentRequestRejected as exc:
            # Rechazo de NEGOCIO (monto fuera de rango, dato faltante) -- error
            # recuperable por el LLM, NUNCA un PluginError acá (auto_disable
            # apagaría pack_payments para TODOS los tenants por esto, ver el
            # docstring de PaymentRequestRejected).
            log.warning("wompi_payment_link_rejected", reference=reference, reason=str(exc))
            return {"error": str(exc)}

        async with get_session() as session:
            if reemplaza is not None:
                # La transacción del link vencido queda borrada (auditoría y webhook
                # tardío por `provider_reference`), nunca dos vivas por referencia.
                # El `where` exige que siga como se leyó: si otra llamada ya la
                # regeneró o un webhook le cambió el estado, no se toca y se decide
                # de nuevo con lo que hay ahora.
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
                    # El link recién creado en Wompi queda sin usar (vence solo).
                    log.warning(
                        "wompi_payment_link_regenerado_en_paralelo",
                        reference=reference,
                        provider_reference_sin_usar=link.provider_reference,
                    )
                    actuales = (
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
                    rechazo = _referencia_no_cobrable(reference, amount, actuales)
                    if rechazo is not None:
                        return rechazo
                    viva = next((t for t in actuales if t.deleted_at is None), None)
                    if viva is not None and not link_vencido(viva, config.link_expira_horas):
                        return _result(viva)
                    return {"error": "No se pudo generar el link de pago ahora. Intentá de nuevo en un momento."}
                log.info(
                    "wompi_payment_link_regenerado",
                    reference=reference,
                    provider_reference_anterior=reemplaza.provider_reference,
                )
            transaction = PaymentTransaction(
                reference=reference,
                provider=provider.provider_name,
                provider_reference=link.provider_reference,
                status="pendiente",
                amount=amount,
                currency=config.currency,
                payment_link_url=link.payment_link_url,
                idempotency_key=context.idempotency_key,
            )
            session.add(transaction)
            await session.flush()
            await session.refresh(transaction)
            return _result(transaction)


# Un link que vence en minutos no sirve para mandárselo al cliente: se trata como vencido.
MARGEN_VENCIMIENTO = timedelta(minutes=15)


def link_vencido(transaction: PaymentTransaction, horas_vigencia: int, ahora: datetime | None = None) -> bool:
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


def _referencia_no_cobrable(
    reference: str, amount: float, transacciones: list[PaymentTransaction]
) -> dict[str, Any] | None:
    """Error recuperable si esta referencia no admite el cobro pedido; `None` si sí.

    Sin `_safe_append`: el modelo no debe ofrecer un link que no corresponde.
    """
    if any(t.status == "aprobado" for t in transacciones):
        return {
            "error": (
                f"La referencia {reference} ya está pagada: no se puede volver a cobrar. "
                "Si el cliente quiere comprar otra cosa, primero hay que crear un pedido nuevo "
                "y cobrar con su referencia."
            ),
            "status": "ya_pagado",
        }
    # También en una transacción ya reemplazada: un webhook tardío del link viejo
    # puede haber dejado plata en revisión.
    if any(t.status == MONTO_DISTINTO for t in transacciones):
        return {
            "error": (
                f"La referencia {reference} tiene un pago en revisión manual (llegó por otro "
                "monto). No generes otro link: el negocio lo está revisando."
            ),
            "status": "en_revision",
        }
    viva = next((t for t in transacciones if t.deleted_at is None), None)
    if viva is None:
        return None
    if abs(Decimal(str(viva.amount)) - Decimal(str(amount))) > Decimal("0.01"):
        return {
            "error": (
                f"La referencia {reference} es un cobro por ${float(viva.amount):,.0f} {viva.currency}, "
                f"no por ${amount:,.0f}. No se generó ningún link. Si es otro pedido, crealo primero "
                "y cobrá con su referencia; si no, usá el monto correcto."
            ),
            "status": "monto_no_coincide",
        }
    return None


def _result(transaction: PaymentTransaction) -> dict[str, Any]:
    return {
        "reference": transaction.reference,
        "status": transaction.status,
        "amount": float(transaction.amount),
        "currency": transaction.currency,
        # SafeRenderer: el link exacto lo adjunta el core -- el LLM no lo copia a mano.
        "_safe_append": f"💳 Podés pagar en línea aquí: {transaction.payment_link_url}",
    }
