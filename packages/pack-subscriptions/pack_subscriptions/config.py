"""Configuración de pack-subscriptions (ADR-0083).

Define cómo se cobra el período y los defaults del plan. La pasarela NO vive acá:
se consume vía `core/payments` (registry) si `pack_payments` está habilitado.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from core.mcp.types import BasePluginConfig


class SubscriptionConfig(BasePluginConfig):
    cobro: str = Field(
        default="link",
        description="Cómo se cobra el período: 'link' (link de pago si hay pasarela) o 'manual'.",
    )
    moneda: str = Field(default="COP", description="Moneda por defecto de los planes.")
    periodicidad: Literal["mensual", "trimestral", "anual"] = Field(
        default="mensual",
        description="Periodicidad por defecto si el plan no la trae (mensual, trimestral o anual).",
    )
    trial_dias: int = Field(default=30, description="Días de prueba por defecto de un plan.")
    dias_gracia: int = Field(
        default=5,
        ge=0,
        le=60,
        description=(
            "Días después del vencimiento de un cargo impago antes de pasar la "
            "suscripción a 'vencida' (D-SPEC-06-3)."
        ),
    )