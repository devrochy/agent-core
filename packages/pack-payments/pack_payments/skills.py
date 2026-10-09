"""PaymentSkill: generación de links de pago en línea."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from core.db.session import get_session
from core.mcp.types import BaseSkill, MCPTool, ScopedHttp, ToolContext
from core.shared.exceptions import PluginError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_payments.config import PaymentsConfig
from pack_payments.models import PaymentTransaction
from pack_payments.providers.base import PaymentRequestRejected
from pack_payments.providers.wompi import WompiProvider

log = get_logger(__name__)

PLUGIN_ID = "pack_payments"


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
                    "ofrecé transferencia o contraentrega en vez de reintentar."
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
        # el mismo tool_call -- eso ya lo cubre el chequeo de arriba), hay que
        # devolver el resultado existente en vez de reventar contra el índice
        # único (tenant_id, reference) -- mismo patrón de bug ya corregido en
        # pack_commerce (excepción no manejada -> PluginError -> auto_disable).
        async with get_session() as session:
            existing_by_reference = (
                await session.execute(
                    select(PaymentTransaction).where(
                        PaymentTransaction.tenant_id == get_current_tenant_id(),
                        PaymentTransaction.reference == reference,
                        PaymentTransaction.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if existing_by_reference is not None:
                return _result(existing_by_reference)

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


def _result(transaction: PaymentTransaction) -> dict[str, Any]:
    return {
        "reference": transaction.reference,
        "status": transaction.status,
        "amount": float(transaction.amount),
        "currency": transaction.currency,
        # SafeRenderer: el link exacto lo adjunta el core -- el LLM no lo copia a mano.
        "_safe_append": f"💳 Podés pagar en línea aquí: {transaction.payment_link_url}",
    }
