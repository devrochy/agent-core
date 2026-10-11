"""Config del tenant y proveedor de pagos, compartidos por la capacidad y el panel.

El pack no expone tools al modelo: el link de un pedido lo pide pack-commerce con
`confirmar_pago_pedido` a través de la `PaymentCapability` (ADR-0087, adenda
2026-10-10). La tool `generar_link_pago`, donde el LLM elegía referencia y monto, se
retiró.
"""

from __future__ import annotations

from core.shared.exceptions import PluginError
from pack_payments.config import PaymentsConfig
from pack_payments.providers.wompi import WompiProvider

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
    """Proveedor con las credenciales del tenant; sale a red por `plugin_http` con la
    allowlist del pack (ADR-0052)."""
    return WompiProvider(
        public_key=config.public_key,
        private_key=config.private_key.get_secret_value(),
        events_secret=config.events_secret.get_secret_value(),
        sandbox=config.sandbox,
    )
