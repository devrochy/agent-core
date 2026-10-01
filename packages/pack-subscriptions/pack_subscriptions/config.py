"""Configuración de pack-subscriptions (ADR-0083).

Define cómo se cobra el período y los defaults del plan. La pasarela NO vive acá:
se consume vía `core/payments` (registry) si `pack_payments` está habilitado.
"""

from __future__ import annotations

from pydantic import Field

from core.mcp.types import BasePluginConfig


class SubscriptionConfig(BasePluginConfig):
    cobro: str = Field(
        default="link",
        description="Cómo se cobra el período: 'link' (link de pago si hay pasarela) o 'manual'.",
    )
    moneda: str = Field(default="COP", description="Moneda por defecto de los planes.")
    periodicidad: str = Field(
        default="mensual",
        description="Periodicidad por defecto de los planes (mensual).",
    )
    trial_dias: int = Field(default=30, description="Días de prueba por defecto de un plan.")