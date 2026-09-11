"""PaymentSkill: generación de links de pago en línea."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from core.db.session import get_session
from core.mcp.types import BaseSkill, MCPTool, ToolContext
from core.shared.exceptions import PluginError
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id
from pack_payments.config import PaymentsConfig
from pack_payments.models import PaymentTransaction
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


def build_provider(config: PaymentsConfig) -> WompiProvider:
    return WompiProvider(
        public_key=config.public_key,
        private_key=config.private_key.get_secret_value(),
        events_secret=config.events_secret.get_secret_value(),
        sandbox=config.sandbox,
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
                    "pedido/cotización)."
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
        amount = float(arguments["amount"])
        if not reference:
            raise PluginError(PLUGIN_ID, "reference vacía")
        if amount <= 0:
            raise PluginError(PLUGIN_ID, "amount debe ser mayor a 0")

        config = await tenant_config()
        provider = build_provider(config)

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

        link = await provider.create_payment_link(
            reference=reference,
            amount=amount,
            currency=config.currency,
            expires_in_hours=config.link_expira_horas,
        )

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
